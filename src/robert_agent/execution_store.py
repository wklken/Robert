"""Caller-transaction SQLite execution controls and publication intents.

Historical workflow tables remain available to the offline upgrade tool only.
Execution ownership is represented by the existing repository leases and attempt
identity; these records preserve recovery limits and stopped-state evidence.
"""

import hashlib
import json

_TERMINAL_TASK_LIFECYCLES = {"completed", "failed", "canceled", "ignored"}
_TERMINAL_EPISODE_STATUSES = {"resolved", "exhausted", "superseded", "canceled"}


class MigrationRequiredError(RuntimeError):
    """Expected upgrade refusal, safe to report at user command boundaries."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_subjects (
  subject_type TEXT NOT NULL CHECK (subject_type IN ('task','remediation_episode')),
  subject_id TEXT NOT NULL,
  workflow_kind TEXT NOT NULL CHECK (workflow_kind IN ('human_request','pr_remediation')),
  binding_status TEXT NOT NULL CHECK (binding_status IN ('active','terminal','attention')),
  recovery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (recovery_attempts BETWEEN 0 AND 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  terminal_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(subject_type,subject_id)
);
CREATE TABLE IF NOT EXISTS execution_pending_facts (
  source_signal_id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  disposition TEXT NOT NULL,
  repo_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  not_before_at TEXT NOT NULL,
  consumed_at TEXT,
  resolution_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS execution_fact_receipts (
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  fact_id TEXT NOT NULL,
  evidence_json TEXT,
  PRIMARY KEY(subject_type,subject_id,fact_id)
);
CREATE TABLE IF NOT EXISTS execution_migrations (
  migration_id TEXT PRIMARY KEY,
  source_fingerprint TEXT NOT NULL UNIQUE,
  applied_at TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  mappings_json TEXT NOT NULL
);
"""


def initialize(conn):
    # execute each statement, preserving a caller-owned transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def get_binding(conn, subject_type, subject_id):
    return _row(
        conn.execute(
            "SELECT * FROM execution_subjects WHERE subject_type=? AND subject_id=?",
            (subject_type, subject_id),
        )
    )


def read_binding(conn, subject_type, subject_id):
    """Read old controls without writing; a neutral row wins per subject."""
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in ("execution_subjects", "workflow_bindings"):
        if table in tables:
            row = _row(
                conn.execute(
                    f"SELECT * FROM {table} WHERE subject_type=? AND subject_id=?",
                    (subject_type, subject_id),
                )
            )
            if row:
                return row
    return None


def read_controls(conn, subjects=None):
    controls = {}
    if subjects is not None:
        subjects = sorted(set(subjects))
        if not subjects:
            return controls
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in ("workflow_bindings", "execution_subjects"):
        if table in tables:
            # Keep bounded parameter counts for older supported SQLite builds.
            batches = [None] if subjects is None else [subjects[i:i + 200] for i in range(0, len(subjects), 200)]
            for batch in batches:
                clause = '' if batch is None else ' WHERE (subject_type,subject_id) IN (' + ','.join('(?,?)' for _ in batch) + ')'
                params = [] if batch is None else [value for subject in batch for value in subject]
                cursor = conn.execute(f"SELECT * FROM {table}" + clause, params)
                names = [column[0] for column in cursor.description]
                for values in cursor.fetchall():
                    row = dict(zip(names, values))
                    controls[(row["subject_type"], row["subject_id"])] = row
    return controls


def ensure_subject(conn, subject_type, subject_id, workflow_kind, now):
    _validate_subject(conn, subject_type, subject_id, workflow_kind)
    conn.execute(
        "INSERT OR IGNORE INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at) VALUES (?,?,?,'active',?,?)",
        (subject_type, subject_id, workflow_kind, now, now),
    )
    row = get_binding(conn, subject_type, subject_id)
    if row["workflow_kind"] != workflow_kind:
        raise ValueError("execution subject kind conflicts with its recorded kind")
    return row


def import_legacy_controls(conn):
    initialize(conn)
    conn.execute("""INSERT OR IGNORE INTO execution_subjects
      (subject_type,subject_id,workflow_kind,binding_status,recovery_attempts,created_at,updated_at,terminal_at,metadata_json)
      SELECT subject_type,subject_id,workflow_kind,binding_status,recovery_attempts,created_at,updated_at,terminal_at,metadata_json
      FROM workflow_bindings WHERE owner_engine='legacy'""")


def migration_coverage(conn):
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    covered_subjects = set()
    covered_signals = set()
    if "execution_migrations" in tables:
        for snapshot, mappings in conn.execute(
            "SELECT snapshot_json,mappings_json FROM execution_migrations"
        ):
            original = json.loads(snapshot)
            covered_subjects.update(
                (b["subject_type"], b["subject_id"])
                for b in original["workflow_bindings"]
                if b["owner_engine"] == "dbos"
            )
            mapped = json.loads(mappings)
            for mapping in mapped:
                if mapping["disposition"] == "already_consumed":
                    receipt = conn.execute(
                        "SELECT 1 FROM workflow_signal_receipts WHERE signal_id=?",
                        (mapping["signal_id"],),
                    ).fetchone()
                    if not receipt:
                        raise MigrationRequiredError(
                            "Migration journal references a missing historical receipt"
                        )
                    continue
                row = _row(
                    conn.execute(
                        "SELECT * FROM execution_pending_facts WHERE source_signal_id=?",
                        (mapping["signal_id"],),
                    )
                )
                if not row or any(
                    row[key] != mapping[key]
                    for key in (
                        "subject_type",
                        "subject_id",
                        "topic",
                        "dedupe_key",
                        "fact_id",
                        "disposition",
                        "repo_id",
                        "task_id",
                        "payload_json",
                        "not_before_at",
                    )
                ):
                    raise MigrationRequiredError(
                        "Migration journal conflicts with queued execution facts"
                    )
            covered_signals.update(m["signal_id"] for m in mapped)
    return covered_subjects, covered_signals


def require_migrated(conn):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='workflow_bindings'"
    ).fetchone():
        return
    covered_subjects, covered_signals = migration_coverage(conn)
    for row in conn.execute(
        "SELECT subject_type,subject_id,binding_status FROM workflow_bindings WHERE owner_engine='dbos'"
    ):
        key = (row[0], row[1])
        if key in covered_subjects:
            if row[2] != "terminal" or not get_binding(conn, *key):
                raise MigrationRequiredError(
                    "DBOS migration journal conflicts with execution controls"
                )
        elif row[2] != "terminal":
            raise MigrationRequiredError(
                "DBOS execution requires offline migration: run robert migrate dbos --db PATH --dry-run"
            )
        elif row[0] == "task":
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id=?", (row[1],)
            ).fetchone()
            if task and task[0] not in _TERMINAL_TASK_LIFECYCLES:
                raise MigrationRequiredError(
                    "Terminal DBOS binding has a nonterminal task; offline migration required"
                )
        else:
            episode = conn.execute(
                "SELECT status FROM pr_remediation_episodes WHERE episode_id=?",
                (row[1],),
            ).fetchone()
            if episode and episode[0] not in _TERMINAL_EPISODE_STATUSES:
                raise MigrationRequiredError(
                    "Terminal DBOS binding has a nonterminal episode; offline migration required"
                )
    for signal, workflow, topic in conn.execute(
        "SELECT signal_id,workflow_id,topic FROM workflow_outbox"
    ):
        receipt = conn.execute(
            "SELECT workflow_id,topic FROM workflow_signal_receipts WHERE signal_id=?",
            (signal,),
        ).fetchone()
        if receipt and tuple(receipt) != (workflow, topic):
            raise MigrationRequiredError(
                "Historical signal receipt conflicts with its source"
            )
        if not receipt and signal not in covered_signals:
            raise MigrationRequiredError(
                "Unconsumed DBOS signal requires offline migration"
            )


def pending_facts(conn, repo_id, now):
    return [
        dict(zip([c[0] for c in cursor.description], row))
        for cursor in [
            conn.execute(
                "SELECT * FROM execution_pending_facts WHERE repo_id=? AND consumed_at IS NULL AND julianday(not_before_at)<=julianday(?) ORDER BY not_before_at,source_signal_id",
                (repo_id, now),
            )
        ]
        for row in cursor.fetchall()
    ]


def consume_fact(conn, source_signal_id, now, reason="domain_effect_recorded"):
    # Call only in the transaction that records the corresponding domain effect.
    conn.execute(
        "UPDATE execution_pending_facts SET consumed_at=?,resolution_json=? WHERE source_signal_id=? AND consumed_at IS NULL",
        (now, _json({"reason": reason}), source_signal_id),
    )
    conn.execute(
        "UPDATE wakeups SET status='consumed',updated_at=? WHERE dedupe_key=? AND status='pending'",
        (now, "dbos-migration:" + source_signal_id),
    )


def record_historical_facts(conn, binding, signals=()):
    metadata = json.loads(binding.get("metadata_json") or "{}")
    latest = metadata.get("latest_fact")
    if isinstance(latest, dict) and latest.get("fact_id"):
        conn.execute(
            "INSERT OR IGNORE INTO execution_fact_receipts VALUES (?,?,?,?)",
            (
                binding["subject_type"],
                binding["subject_id"],
                latest["fact_id"],
                _json(latest),
            ),
        )
    for signal in signals:
        if (
            signal["workflow_id"] == binding.get("workflow_id")
            and signal["topic"] == "subject-events"
        ):
            payload = json.loads(signal["payload_json"])
            if payload.get("fact_id"):
                conn.execute(
                    "INSERT OR IGNORE INTO execution_fact_receipts VALUES (?,?,?,NULL)",
                    (
                        binding["subject_type"],
                        binding["subject_id"],
                        payload["fact_id"],
                    ),
                )


def runnable_sql(alias="t"):
    return f"""(json_extract({alias}.metadata_json,'$.execution_attention') IS NULL
      AND NOT EXISTS (SELECT 1 FROM execution_subjects control WHERE
      control.binding_status='attention' AND
      ((control.subject_type='task' AND control.subject_id={alias}.task_id) OR
       (control.subject_type='remediation_episode' AND control.subject_id=json_extract({alias}.metadata_json,'$.remediation_episode_id')))))"""


_PUBLICATION_TRANSITIONS = {
    "prepared": {"executing"},
    "executing": {"published", "confirmed_absent", "failed", "attention"},
    "confirmed_absent": {"executing"},
    "failed": {"executing"},
    "published": set(),
    "attention": set(),
}


def _row(cursor):
    value = cursor.fetchone()
    if value is None:
        return None
    return dict(zip((column[0] for column in cursor.description), value))


def _json(value):
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _id(prefix, *values):
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _validate_subject(conn, subject_type, subject_id, workflow_kind):
    legal_pairs = {
        ("task", "human_request"),
        ("remediation_episode", "pr_remediation"),
    }
    if (subject_type, workflow_kind) not in legal_pairs:
        raise ValueError("illegal workflow subject and kind pairing")
    if subject_type == "task":
        row = conn.execute(
            "SELECT task_kind, lifecycle FROM tasks WHERE task_id = ?", (subject_id,)
        ).fetchone()
        if not row:
            raise ValueError("workflow task subject does not exist")
        if row[0] != "human_request" or row[1] in _TERMINAL_TASK_LIFECYCLES:
            raise ValueError("workflow task subject is not eligible")
        return
    row = conn.execute(
        "SELECT status FROM pr_remediation_episodes WHERE episode_id = ?", (subject_id,)
    ).fetchone()
    if not row:
        raise ValueError("workflow remediation episode subject does not exist")
    if row[0] in _TERMINAL_EPISODE_STATUSES:
        raise ValueError("workflow remediation episode subject is not eligible")


def close_binding(conn, subject_type, subject_id, now):
    binding = get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    if binding["binding_status"] == "terminal":
        return binding
    conn.execute(
        """
        UPDATE execution_subjects
        SET binding_status = 'terminal', terminal_at = ?, updated_at = ?
        WHERE subject_type = ? AND subject_id = ?
        """,
        (now, now, subject_type, subject_id),
    )
    return get_binding(conn, subject_type, subject_id)


def mark_binding_attention(conn, subject_type, subject_id, now):
    binding = get_binding(conn, subject_type, subject_id)
    if not binding:
        raise ValueError("workflow subject is not bound")
    if binding["binding_status"] == "attention":
        return binding
    if binding["binding_status"] == "terminal":
        raise ValueError("terminal workflow binding cannot require attention")
    conn.execute(
        """
        UPDATE execution_subjects
        SET binding_status = 'attention', updated_at = ?
        WHERE subject_type = ? AND subject_id = ?
        """,
        (now, subject_type, subject_id),
    )
    return get_binding(conn, subject_type, subject_id)


def prepare_publication_intent(
    conn, action_id, idempotency_key, request_fingerprint, now
):
    if not conn.execute(
        "SELECT 1 FROM github_actions WHERE action_id = ?", (action_id,)
    ).fetchone():
        raise ValueError("publication action does not exist")
    existing = _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE action_id = ?", (action_id,)
        )
    )
    if existing:
        if (
            existing["idempotency_key"] == idempotency_key
            and existing["request_fingerprint"] == request_fingerprint
        ):
            return existing
        raise ValueError("publication action already has a conflicting intent")
    existing_key = _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE idempotency_key = ?",
            (idempotency_key,),
        )
    )
    if existing_key:
        raise ValueError(
            "publication idempotency key already belongs to another action"
        )
    publication_id = _id("publication", action_id)
    conn.execute(
        """
        INSERT INTO publication_intents(
          publication_id, action_id, idempotency_key, request_fingerprint,
          status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'prepared', ?, ?)
        """,
        (publication_id, action_id, idempotency_key, request_fingerprint, now, now),
    )
    return _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE publication_id = ?",
            (publication_id,),
        )
    )


def transition_publication_intent(
    conn, publication_id, status, now, remote_evidence=None, error=None
):
    intent = _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE publication_id = ?",
            (publication_id,),
        )
    )
    if not intent:
        raise ValueError("publication intent does not exist")
    if intent["status"] == status:
        return intent
    if status not in _PUBLICATION_TRANSITIONS.get(intent["status"], set()):
        raise ValueError(
            f"illegal publication intent transition: {intent['status']} -> {status}"
        )
    fields = ["status = ?", "updated_at = ?"]
    values = [status, now]
    if status == "executing":
        fields.append("started_at = COALESCE(started_at, ?)")
        values.append(now)
        fields.append("finished_at = NULL")
        fields.append("error_json = NULL")
    if status in {"published", "confirmed_absent", "failed", "attention"}:
        fields.append("finished_at = ?")
        values.append(now)
    if remote_evidence is not None:
        fields.append("remote_evidence_json = ?")
        values.append(_json(remote_evidence))
    if error is not None:
        fields.append("error_json = ?")
        values.append(_json(error))
    values.append(publication_id)
    conn.execute(
        f"UPDATE publication_intents SET {', '.join(fields)} WHERE publication_id = ?",
        values,
    )
    return _row(
        conn.execute(
            "SELECT * FROM publication_intents WHERE publication_id = ?",
            (publication_id,),
        )
    )

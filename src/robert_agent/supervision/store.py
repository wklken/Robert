"""Caller-transaction storage for supervision observations and incidents."""

import json
from uuid import uuid4

from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision.rules import timestamp


_ACTIVE = "status NOT IN ('resolved','dismissed')"
_SCOPE = "scope_type=? AND scope_id=? AND rule_id=?"


def has_schema(conn):
    """Report whether the built-in supervision migration completed."""
    tables = {
        row[0]
        for row in conn.execute("""SELECT name FROM sqlite_master
        WHERE type='table' AND name IN ('schema_migrations','supervision_checkpoints',
                                       'supervision_incidents','supervision_recovery_actions')""")
    }
    if len(tables) != 4:
        return False
    row = conn.execute(
        "SELECT checksum FROM schema_migrations WHERE name=?",
        (storage.SUPERVISION_MIGRATION_NAME,),
    ).fetchone()
    return row is not None and row[0] == storage.SUPERVISION_MIGRATION_CHECKSUM


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decode(columns, row):
    result = {}
    for (name, *_), value in zip(columns, row):
        if name.endswith("_json"):
            result[name[:-5]] = json.loads(value)
        elif name == "lineage_key":
            result[name] = value or None
        else:
            result[name] = value
    return result


def _one(cursor):
    row = cursor.fetchone()
    return _decode(cursor.description, row) if row is not None else None


def list_incidents(conn, status=None, limit=100):
    """List recent incidents. `active` includes every unresolved status."""
    where, params = ("", []) if status is None else ("WHERE status=?", [status])
    if status == "active":
        where, params = (f"WHERE {_ACTIVE}", [])
    cursor = conn.execute(
        f"""SELECT * FROM supervision_incidents {where}
        ORDER BY last_observed_at DESC, incident_id DESC LIMIT ?""",
        (*params, max(0, min(limit, 1000))),
    )
    return [_decode(cursor.description, row) for row in cursor]


def get_checkpoint(conn, scope_type, scope_id, rule_id):
    return _one(
        conn.execute(
            f"SELECT * FROM supervision_checkpoints WHERE {_SCOPE}",
            (scope_type, scope_id, rule_id),
        )
    )


def update_checkpoint(conn, scope_type, scope_id, rule_id, *, now, metadata):
    """Replace metadata owned by a named checkpoint without replacing its observation."""
    conn.execute(
        """INSERT INTO supervision_checkpoints(scope_type,scope_id,rule_id,updated_at,metadata_json)
        VALUES (?,?,?,?,?) ON CONFLICT(scope_type,scope_id,rule_id) DO UPDATE SET
        updated_at=excluded.updated_at,metadata_json=excluded.metadata_json
        WHERE supervision_checkpoints.metadata_json IS NOT excluded.metadata_json""",
        (scope_type, scope_id, rule_id, now, _json(metadata)),
    )


def _record_observation(conn, key, observation, now):
    progress_token = observation.get("progress_token")
    if progress_token is not None and not isinstance(progress_token, str):
        progress_token = _json(progress_token)
    conn.execute(
        """INSERT INTO supervision_checkpoints(
            scope_type,scope_id,rule_id,lineage_key,classification,reason_code,
            evidence_json,progress_token,last_progress_at,next_check_at,updated_at,eligible_since)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(scope_type,scope_id,rule_id) DO UPDATE SET
          last_progress_at=CASE
            WHEN supervision_checkpoints.lineage_key IS NOT excluded.lineage_key THEN excluded.last_progress_at
            WHEN excluded.progress_token IS NOT NULL AND supervision_checkpoints.progress_token IS NOT excluded.progress_token
              THEN excluded.last_progress_at ELSE supervision_checkpoints.last_progress_at END,
          progress_token=CASE WHEN supervision_checkpoints.lineage_key IS NOT excluded.lineage_key
            THEN excluded.progress_token ELSE COALESCE(excluded.progress_token,supervision_checkpoints.progress_token) END,
          lineage_key=excluded.lineage_key,classification=excluded.classification,
          reason_code=excluded.reason_code,evidence_json=excluded.evidence_json,
          next_check_at=excluded.next_check_at,updated_at=excluded.updated_at,
          eligible_since=excluded.eligible_since
        WHERE supervision_checkpoints.lineage_key IS NOT excluded.lineage_key
           OR supervision_checkpoints.classification IS NOT excluded.classification
           OR supervision_checkpoints.reason_code IS NOT excluded.reason_code
           OR supervision_checkpoints.evidence_json IS NOT excluded.evidence_json
           OR supervision_checkpoints.next_check_at IS NOT excluded.next_check_at
           OR supervision_checkpoints.eligible_since IS NOT excluded.eligible_since
           OR (excluded.progress_token IS NOT NULL AND supervision_checkpoints.progress_token IS NOT excluded.progress_token)""",
        (
            *key,
            observation["classification"],
            observation.get("reason_code"),
            _json(observation.get("evidence") or {}),
            progress_token,
            (observation.get("progress_observed_at") or now)
            if progress_token is not None
            else None,
            observation.get("next_check_at"),
            now,
            observation.get("eligible_since"),
        ),
    )


def _confirmed(conn, key, observation, active, now):
    values = (
        observation["reason_code"],
        observation["severity"],
        _json(observation["evidence"]),
        _json(observation["expected_progress"]),
        now,
        observation.get("next_check_at"),
    )
    if active:
        conn.execute(
            """UPDATE supervision_incidents SET reason_code=?,severity=?,evidence_json=?,
            expected_progress_json=?,last_observed_at=?,next_check_at=? WHERE incident_id=?""",
            (*values, active[0]),
        )
        return None
    previous = conn.execute(
        f"""SELECT incident_id FROM supervision_incidents
        WHERE {_SCOPE} AND lineage_key=? ORDER BY first_observed_at DESC, rowid DESC LIMIT 1""",
        key,
    ).fetchone()
    incident_id = f"incident-{uuid4().hex}"
    conn.execute(
        """INSERT INTO supervision_incidents(
        incident_id,scope_type,scope_id,rule_id,lineage_key,status,reason_code,severity,evidence_json,
        expected_progress_json,last_observed_at,next_check_at,first_observed_at,prior_incident_id)
        VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
        (incident_id, *key, *values, now, previous[0] if previous else None),
    )
    return incident_id


def apply_observations(conn, observations, now):
    """Persist validated observations and return incident transitions.

    The caller holds a writer transaction from revalidation through commit.
    Evidence must already be bounded and redacted. A rule can resolve an
    incident with `healthy` and timestamped positive domain evidence for the
    same scope, rule and lineage. Omitting an observation does nothing.
    """
    changes = []
    for observation in observations:
        key = (
            observation["scope_type"],
            observation["scope_id"],
            observation["rule_id"],
            observation.get("lineage_key") or "",
        )
        _record_observation(conn, key, observation, now)
        classification = observation["classification"]
        positive = observation.get("positive_evidence")
        occurred_at = (
            timestamp(positive.get("occurred_at"))
            if isinstance(positive, dict)
            else None
        )
        can_resolve = (
            classification == "healthy"
            and occurred_at is not None
            and occurred_at <= timestamp(now)
        )
        if classification != "confirmed" and not can_resolve:
            continue
        active = conn.execute(
            f"""SELECT incident_id,status,expected_progress_json,first_observed_at
            FROM supervision_incidents WHERE {_SCOPE} AND lineage_key=? AND {_ACTIVE}""",
            key,
        ).fetchone()
        if classification == "confirmed":
            incident_id = _confirmed(conn, key, observation, active, now)
            if incident_id is None:
                continue
            transition = "opened"
        elif active and active[1] in {"open", "diagnosing", "needs_attention"}:
            if observation["rule_id"] == "ready_not_dispatched":
                expected_attempt = json.loads(active[2]).get("attempt_id")
                opened_at = timestamp(active[3])
                if (
                    not expected_attempt
                    or positive.get("attempt_id") != expected_attempt
                    or not opened_at
                    or occurred_at < opened_at
                ):
                    continue
            incident_id = active[0]
            conn.execute(
                """UPDATE supervision_incidents SET status='resolved',resolved_by='natural',
                resolved_at=?,last_observed_at=?,next_check_at=NULL,resolution_evidence_json=? WHERE incident_id=?""",
                (now, now, _json(positive), incident_id),
            )
            transition = "resolved"
        else:
            continue
        incident = _one(
            conn.execute(
                "SELECT * FROM supervision_incidents WHERE incident_id=?",
                (incident_id,),
            )
        )
        changes.append({"transition": transition, "incident": incident})
    return changes


def repair_task(conn, task_id):
    """Return automatic maintenance provenance, if any, for a dispatch target."""
    row = conn.execute(
        "SELECT json_extract(metadata_json,'$.supervision_repair') FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else None

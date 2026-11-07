"""Run a bounded, advisory diagnosis outside the business worker queue."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from robert_agent.security import redaction
from robert_agent.supervision import rules as supervision_rules
from robert_agent.supervision import store as supervision_store
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.configuration.supervision import normalize_diagnosis
from robert_agent.worker_adapters import build_worker_launch


_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|token|secret|credentials?|api_key|authorization|cookie)(?:$|_)",
    re.I,
)
_DIAGNOSIS_CLASSES = {
    "transient_failure",
    "expected_wait",
    "repeated_failure",
    "goal_deviation",
    "product_defect",
    "operator_needed",
    "unknown",
}
_RECOMMENDATIONS = {"wait", "reconcile", "repair_task", "ask_user"}
_OUTPUT_FIELDS = {
    "bundle_id",
    "bundle_sha256",
    "diagnosis_class",
    "evidence_ids",
    "recommended_action",
    "expected_progress",
}
_MAX_STDOUT = 8192
_MAX_BUNDLE = 49152
_RUNNER = ("diagnostic", "installation", "runner")
_IN_PROGRESS = ("open", "needs_attention", "diagnosing")
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _safe(value, depth=0):
    if isinstance(value, str):
        result = redaction.redact_and_truncate(value, 1800)
        return result["text"] if result["ok"] else "[omitted: credential pattern]"
    if value is None or type(value) in (bool, int, float):
        return value
    if depth >= 4:
        return "[omitted: nested evidence]"
    if isinstance(value, dict):
        return {
            _safe(key): _safe(child, depth + 1)
            for key, child in list(value.items())[:24]
            if not _SENSITIVE_KEY.search(key)
        }
    return [_safe(child, depth + 1) for child in value[:8]]


def _structured(row, fields):
    value = dict(row)
    for key in fields:
        if isinstance(value.get(key), str):
            value[key] = json.loads(value[key])
    return value


def _connect(db_path, *, readonly=False):
    mode = "ro" if readonly else "rw"
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode={mode}", uri=True, timeout=0.05)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _write(db_path):
    with closing(_connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def _checkpoint(conn, key):
    return (supervision_store.get_checkpoint(conn, *key) or {}).get("metadata", {})


def _put(conn, key, metadata, now):
    supervision_store.update_checkpoint(
        conn, *key, now=now.isoformat(), metadata=metadata
    )


def _target(row, source):
    identifier = (
        row["incident_id"]
        if source == "incident"
        else f"candidate:{row['scope_id']}:{row['rule_id']}"
    )
    lineage = row["lineage_key"] or f"{row['scope_type']}:{row['scope_id']}"
    return {
        "source": source,
        "id": identifier,
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "rule_id": row["rule_id"],
        "lineage": lineage,
    }


def _analysis_key(target):
    return "diagnosis", target["id"], "analysis"


def _cooldown_key(target):
    return "diagnostic", _digest(target["lineage"]), "cooldown"


def _targets(conn, now):
    cutoff = now.astimezone(timezone.utc).isoformat()
    incidents = conn.execute(
        """SELECT i.incident_id,i.scope_type,i.scope_id,i.rule_id,i.lineage_key
        FROM supervision_incidents i LEFT JOIN supervision_checkpoints c
          ON c.scope_type='diagnosis' AND c.scope_id=i.incident_id AND c.rule_id='analysis'
        WHERE i.status IN ('open','needs_attention','diagnosing')
          AND COALESCE(json_extract(c.metadata_json,'$.evidence_retry_at'),'') <= ?
        ORDER BY COALESCE(c.updated_at,''),i.first_observed_at,i.incident_id LIMIT 32""",
        (cutoff,),
    ).fetchall()
    candidates = conn.execute(
        """SELECT c.scope_type,c.scope_id,c.rule_id,c.lineage_key
        FROM supervision_checkpoints c JOIN tasks t ON t.task_id=c.scope_id
        LEFT JOIN supervision_checkpoints d ON d.scope_type='diagnosis'
          AND d.scope_id='candidate:' || c.scope_id || ':' || c.rule_id AND d.rule_id='analysis'
        WHERE c.scope_type='task' AND c.classification='suspect' AND t.lifecycle='running'
          AND COALESCE(json_extract(d.metadata_json,'$.evidence_retry_at'),'') <= ?
        ORDER BY COALESCE(d.updated_at,''),c.last_progress_at,c.scope_id,c.rule_id LIMIT 32""",
        (cutoff,),
    ).fetchall()
    return [_target(row, "incident") for row in incidents] + [
        _target(row, "checkpoint") for row in candidates
    ]


def _read_subject(conn, target):
    if target["source"] == "incident":
        row = conn.execute(
            """SELECT scope_type,scope_id,rule_id,lineage_key,status,reason_code,
                CASE WHEN length(evidence_json)<=12000 THEN evidence_json END AS evidence_json,
                CASE WHEN length(expected_progress_json)<=3000 THEN expected_progress_json END AS expected_json
            FROM supervision_incidents WHERE incident_id=?""",
            (target["id"],),
        ).fetchone()
        if not row or row["status"] not in _IN_PROGRESS:
            return None
    else:
        row = conn.execute(
            """SELECT c.scope_type,c.scope_id,c.rule_id,c.lineage_key,c.classification AS status,c.reason_code,
                CASE WHEN length(c.evidence_json)<=12000 THEN c.evidence_json END AS evidence_json,
                NULL AS expected_json
            FROM supervision_checkpoints c JOIN tasks t ON t.task_id=c.scope_id
            WHERE c.scope_type=? AND c.scope_id=? AND c.rule_id=? AND t.lifecycle='running' """,
            (target["scope_type"], target["scope_id"], target["rule_id"]),
        ).fetchone()
        if not row or row["status"] != "suspect":
            return None
    lineage = row["lineage_key"] or f"{row['scope_type']}:{row['scope_id']}"
    if (
        lineage != target["lineage"]
        or row["scope_type"] != target["scope_type"]
        or row["scope_id"] != target["scope_id"]
        or row["rule_id"] != target["rule_id"]
    ):
        return None
    return _structured(row, ("evidence_json", "expected_json"))


def _task_context(conn, target):
    if target["scope_type"] != "task":
        return None, None, None
    task = conn.execute(
        """SELECT t.task_id,t.lifecycle,t.route_id,t.expected_output,t.workstream_id,
            t.updated_at,w.repo_id,r.full_name AS repository,r.enabled AS repository_enabled,
            (SELECT allowed_github_actions_json FROM route_decisions WHERE task_id=t.task_id
             ORDER BY created_at DESC,route_decision_id DESC LIMIT 1) AS route_actions
        FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
        JOIN repos r ON r.repo_id=w.repo_id WHERE t.task_id=?""",
        (target["scope_id"],),
    ).fetchone()
    if not task:
        return None, None, None
    item = conn.execute(
        """WITH RECURSIVE origins(stream_id,depth) AS (
            SELECT ?,0 UNION ALL
            SELECT w.origin_workstream_id,o.depth+1 FROM origins o JOIN workstreams w ON w.workstream_id=o.stream_id
            WHERE w.origin_workstream_id IS NOT NULL AND w.repo_id=? AND o.depth<7
        ) SELECT wi.work_item_id,wi.title,wi.version,wi.origin_type,
                 CASE WHEN length(wi.description)<=8000 THEN wi.description END AS description
        FROM origins o JOIN work_items wi ON wi.workstream_id=o.stream_id
        WHERE wi.repo_id=? ORDER BY o.depth LIMIT 1""",
        (task["workstream_id"], task["repo_id"], task["repo_id"]),
    ).fetchone()
    attempt = conn.execute(
        """SELECT a.attempt_id,a.status,a.branch_name,a.started_at,a.finished_at,
            json_extract(a.metadata_json,'$.existing_pr_head_sha') AS input_sha,
            wr.result_id,wr.output_type,json_extract(wr.metadata_json,'$.audit.status') AS audit_status,
            json_extract(wr.metadata_json,'$.audit.action_scope.result_head_sha') AS result_sha,
            CASE WHEN length(wr.verification_json)<=8000 THEN wr.verification_json END AS reported_verification
        FROM attempts a LEFT JOIN worker_results wr ON wr.attempt_id=a.attempt_id AND wr.task_id=a.task_id
        WHERE a.task_id=? ORDER BY a.attempt_no DESC LIMIT 1""",
        (task["task_id"],),
    ).fetchone()
    return (
        _structured(task, ("route_actions",)),
        dict(item) if item else None,
        _structured(attempt, ("reported_verification",)) if attempt else None,
    )


def _snapshot(conn, target):
    subject = _read_subject(conn, target)
    if subject is None:
        return None
    task, item, attempt = _task_context(conn, target)
    if target["scope_type"] == "task" and not task:
        return None
    facts = {
        "observation": subject,
        "task": task,
        "work_item": item,
        "attempt": attempt,
    }
    if task:
        events = conn.execute(
            """SELECT ge.event_type,ge.event_at,ge.authorization_status,
                CASE WHEN length(json_extract(ge.payload_json,'$.title'))<=4000
                     THEN json_extract(ge.payload_json,'$.title') END AS title,
                CASE WHEN length(json_extract(ge.payload_json,'$.body'))<=8000
                     THEN json_extract(ge.payload_json,'$.body') END AS body
            FROM task_events te JOIN github_events ge ON ge.event_id=te.event_id
            WHERE te.task_id=? AND ge.repo_id=? AND te.relationship IN ('trigger','pending','consumed')
            ORDER BY te.relationship='trigger' DESC,ge.event_at DESC,ge.event_id DESC LIMIT 3""",
            (task["task_id"], task["repo_id"]),
        ).fetchall()
        facts["request_events"] = [dict(row) for row in events]
    if attempt:
        phases = conn.execute(
            """SELECT phase,status,created_at,
                CASE WHEN length(summary)<=4000 THEN summary END AS summary
            FROM worker_phases WHERE attempt_id=? ORDER BY created_at DESC,phase_id DESC LIMIT 3""",
            (attempt["attempt_id"],),
        ).fetchall()
        facts["worker_phases"] = [dict(row) for row in phases]
    return _safe(facts, -1)


def _bundle(snapshot, target, bundle_id):
    evidence = []
    missing = [
        "current_external_authorization",
        "deployed_build",
        "complete_worker_tool_trace",
    ]
    for kind, value in snapshot.items():
        if value is None:
            if target["scope_type"] == "task":
                missing.append(kind)
            continue
        evidence.append({"id": f"e{len(evidence) + 1}", "kind": kind, "data": value})
    packet = {
        "bundle_id": bundle_id,
        "input_generation": _digest(snapshot),
        "scope": _safe(target),
        "evidence": evidence,
        "missing": missing,
        "instructions": files("robert_agent.resources")
        .joinpath("supervision-diagnosis.md")
        .read_text(encoding="utf-8"),
    }
    return {**packet, "bundle_sha256": _digest(packet)}


def _status(status, target=None, *, ok=True):
    result = {"ok": ok, "status": status}
    if target:
        result["incident_id" if target["source"] == "incident" else "candidate_id"] = (
            target["id"]
        )
    return result


def _set_claim(db_path, claim, now):
    with _write(db_path) as conn:
        runner = _checkpoint(conn, _RUNNER)
        if (runner.get("claim") or {}).get("job_id") != claim["job_id"]:
            return False
        _put(conn, _RUNNER, {**runner, "claim": claim}, now)
    return True


def _complete(
    db_path, claim, now, status, diagnosis=None, *, stopped=True, provider_error=None
):
    target = claim["target"]
    with _write(db_path) as conn:
        runner = _checkpoint(conn, _RUNNER)
        if (runner.get("claim") or {}).get("job_id") != claim["job_id"]:
            return _status("claim_changed", target, ok=False)
        if (
            diagnosis is not None
            and _digest(_snapshot(conn, target)) != claim["input_generation"]
        ):
            status, diagnosis = "stale_evidence", None
        record = _checkpoint(conn, _analysis_key(target))
        if record.get("bundle_id") != claim["bundle_id"]:
            return _status("claim_changed", target, ok=False)
        record.update(status=status, observed_at=now.isoformat())
        if diagnosis is not None:
            record["diagnosis"] = diagnosis
        if provider_error is not None:
            delay = max(
                claim["cooldown_seconds"], provider_error.get("retry_after_seconds", 0)
            )
            retry_at = (now + timedelta(seconds=delay)).isoformat()
            record["provider_error"] = {**provider_error, "retry_at": retry_at}
            runner["provider_not_before"] = retry_at
        _put(conn, _analysis_key(target), record, now)
        _put(
            conn,
            _RUNNER,
            {**runner, "claim": None if stopped else {**claim, "status": "attention"}},
            now,
        )
    return _status(
        status,
        target,
        ok=status in {"succeeded", "interrupted", "stopped", "stale_evidence"},
    )


def _validate_output(raw, claim):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    output = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(output, dict) or set(output) not in (
        _OUTPUT_FIELDS,
        {"bundle_id", "bundle_sha256", "error"},
    ):
        raise ValueError("wrong diagnosis fields")
    if (
        output["bundle_id"] != claim["bundle_id"]
        or output["bundle_sha256"] != claim["bundle_sha256"]
    ):
        raise ValueError("diagnosis references a different bundle")
    if "error" in output:
        error = output["error"]
        if (
            not isinstance(error, dict)
            or set(error) - {"code", "retry_after_seconds"}
            or error.get("code") != "rate_limited"
            or type(error.get("retry_after_seconds", 0)) is not int
            or not 0 <= error.get("retry_after_seconds", 0) <= 86400
        ):
            raise ValueError("invalid provider rate limit")
        return output
    for key, values in (
        ("diagnosis_class", _DIAGNOSIS_CLASSES),
        ("recommended_action", _RECOMMENDATIONS),
    ):
        if not isinstance(output[key], str) or output[key] not in values:
            raise ValueError("unsupported diagnosis class or recommendation")
    ids = output["evidence_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= len(claim["evidence_ids"])
        or any(
            not isinstance(identifier, str) or identifier not in claim["evidence_ids"]
            for identifier in ids
        )
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("diagnosis references unknown or duplicate evidence")
    expected = output["expected_progress"]
    if not isinstance(expected, str) or not expected.strip() or len(expected) > 1000:
        raise ValueError("expected progress is missing or too large")
    safe = redaction.redact_text(expected)
    if not safe["ok"]:
        raise ValueError("diagnosis contains sensitive text")
    safe["text"].encode("utf-8")  # JSON escapes can still decode to invalid Unicode.
    return {**output, "expected_progress": safe["text"]}


def _output(proc, deadline, stop):
    output = bytearray()
    eof = False
    with selectors.DefaultSelector() as selector:
        descriptor = proc.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timed_out", b""
            if eof:
                if proc.poll() is not None:
                    return ("exited" if proc.returncode else "read"), bytes(output)
                stop.wait(min(remaining, 0.05))
            elif selector.select(min(remaining, 0.05)):
                chunk = os.read(descriptor, min(4096, _MAX_STDOUT + 1 - len(output)))
                if chunk:
                    output.extend(chunk)
                    if len(output) > _MAX_STDOUT:
                        return "invalid_output", b""
                else:
                    eof = True
    return "stopped", b""


def _stable_identity(proc, stop):
    # On macOS `ps` can observe the exec transition before argv stabilizes.
    # Never persist that transient command as proof for restart or cleanup.
    previous = None
    for _ in range(5):
        if proc.poll() is not None:
            return None
        try:
            observed = worker_identity.capture_process_identity(proc.pid)
        except (OSError, worker_identity.IdentityReadError):
            observed = None
        if (
            observed
            and previous
            and worker_identity.compare_recorded_identity(previous, observed)
            == "alive_same"
        ):
            return observed
        previous = observed
        if stop.wait(0.025):
            return None
    if proc.poll() is None:
        raise ValueError("diagnosis process identity did not stabilize")
    return None


def _run(db_path, claim, packet, profile, now, stop, *, observed_monotonic):
    started = time.monotonic()
    status, diagnosis, proc, stopped = "stopped", None, None, True
    provider_error = None
    try:
        directory = tempfile.TemporaryDirectory(
            prefix="robert-diagnosis-", ignore_cleanup_errors=True
        )
    except OSError:
        return _PendingCompletion(db_path, claim, now, "launch_failed", None, True)
    with directory as root:
        try:
            cwd = Path(root).resolve()
            source = cwd / "request.json"
            source.write_text(_json(packet), encoding="utf-8")
            launch = build_worker_launch(
                source,
                cwd,
                worker_agent_config={
                    "agent": "command",
                    "command": profile["command"],
                    "environment_allowlist": profile["environment_allowlist"],
                    "timeout_seconds": profile["timeout_seconds"],
                },
            )
        except (OSError, ValueError):
            return _PendingCompletion(db_path, claim, now, "launch_failed", None, True)
        environment = {
            name: os.environ[name]
            for name in launch.environment_allowlist
            if name in os.environ
        }
        environment.update(HOME=str(cwd), TMPDIR=str(cwd), PATH=os.defpath)
        if not stop.is_set():
            try:
                with source.open("rb") as input_stream:
                    proc = subprocess.Popen(
                        launch.command,
                        cwd=launch.cwd,
                        env=environment,
                        stdin=input_stream,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                if identity := _stable_identity(proc, stop):
                    if identity["process_group_id"] != proc.pid:
                        raise ValueError("diagnosis process group is not isolated")
                    claim = {**claim, "process_identity": identity, "status": "running"}
                    if not _set_claim(db_path, claim, now):
                        raise ValueError("diagnosis claim changed during spawn")
                status, output = _output(proc, started + launch.timeout_seconds, stop)
                if status in {"read", "exited"} and output:
                    try:
                        validated = _validate_output(output, claim)
                        if "error" in validated:
                            status, provider_error = (
                                "provider_rate_limited",
                                validated["error"],
                            )
                        elif status == "read":
                            status, diagnosis = "succeeded", validated
                    except (ValueError, RecursionError):
                        status = "invalid_output"
                elif status == "read":
                    status = "invalid_output"
            except (
                OSError,
                sqlite3.Error,
                ValueError,
                worker_identity.IdentityReadError,
            ):
                status = "launch_failed" if proc is None else "process_attention"
            finally:
                if proc is not None:
                    proc.stdout.close()
                    try:
                        stopped = (
                            proc.poll() is not None
                            and not worker_identity.live_process_group(proc.pid)
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        stopped = False
                    if not stopped:
                        stopped = worker_identity.cleanup_new_process(
                            proc, wait_timeout=0.5
                        )
            if not stopped:
                status, diagnosis, provider_error = "process_attention", None, None
    finished = now + timedelta(seconds=time.monotonic() - observed_monotonic)
    return _PendingCompletion(
        db_path,
        claim,
        finished,
        status,
        diagnosis,
        stopped,
        provider_error=provider_error,
    )


def _recover_previous(db_path, claim, now, stop):
    target = claim["target"]
    identity = claim.get("process_identity")
    if not identity:
        return _status("process_attention", target, ok=False)
    state = worker_identity.inspect_recorded_identity(identity)
    pgid = identity["process_group_id"]
    deadline = supervision_rules.timestamp(claim["deadline_at"])
    timed_out = deadline is not None and now >= deadline
    if state == "alive_same" and not timed_out:
        return _status("previous_process_running", target)
    if state == "alive_same" and not stop.is_set():
        terminated = worker_identity.terminate_recorded_identity(
            identity, timeout_seconds=0.5
        )
        if terminated["status"] in {"terminated", "already_dead"}:
            state = "dead"
    if state == "dead" and not worker_identity.live_process_group(pgid):
        return _complete(
            db_path, claim, now, "timed_out" if timed_out else "interrupted"
        )
    return _status("process_attention", target, ok=False)


class _PendingCompletion:
    """Keep an already reaped outcome until its durable write succeeds."""

    def __init__(
        self, db_path, claim, now, status, diagnosis, stopped, *, provider_error=None
    ):
        self.provider_error = provider_error
        self.arguments = (db_path, claim, now, status, diagnosis)
        self.stopped = stopped

    def persist(self):
        return _complete(
            *self.arguments, stopped=self.stopped, provider_error=self.provider_error
        )


def _persist_job(job):
    if isinstance(job.result, _PendingCompletion):
        try:
            job.result = job.result.persist()
        except (OSError, sqlite3.Error):
            return _status("completion_pending", ok=False)
    return job.result or _status("unavailable", ok=False)


class _Job:
    def __init__(self, callback):
        self.stop = threading.Event()
        self.result = None

        def execute():
            try:
                self.result = callback(self.stop)
            except (OSError, sqlite3.Error, ValueError, subprocess.SubprocessError):
                self.result = _status("unavailable", ok=False)

        self.thread = threading.Thread(
            target=execute, name="robert-diagnosis", daemon=True
        )
        self.thread.start()


def _provider_wait(runner, now):
    deadline = supervision_rules.timestamp(runner.get("provider_not_before"))
    return deadline is not None and now < deadline


def _claim(db_path, profile, target, packet, packet_bytes, now):
    with _write(db_path) as conn:
        runner = _checkpoint(conn, _RUNNER)
        if runner.get("claim"):
            return None, "claim_exists"
        if _provider_wait(runner, now):
            return None, "provider_cooldown"
        hour_ago = now - timedelta(hours=1)
        recent = [
            stamp
            for stamp in runner.get("hourly_calls", [])
            if (parsed := supervision_rules.timestamp(stamp)) is not None
            and parsed > hour_ago
        ]
        if len(recent) >= profile["max_calls_per_hour"]:
            return None, "rate_limited"
        key = _cooldown_key(target)
        last = supervision_rules.timestamp(_checkpoint(conn, key).get("claimed_at"))
        if last is not None and now < last + timedelta(
            seconds=profile["cooldown_seconds"]
        ):
            return None, "cooldown"
        analysis_key = _analysis_key(target)
        retry_at = supervision_rules.timestamp(
            _checkpoint(conn, analysis_key).get("evidence_retry_at")
        )
        if retry_at is not None and now < retry_at:
            return None, "cooldown"
        if _digest(_snapshot(conn, target)) != packet["input_generation"]:
            return None, "stale_evidence"
        if packet_bytes > _MAX_BUNDLE:
            # This rejection belongs to one target. No wrapper ran, so it
            # does not spend a call or cool down the rest of the lineage.
            retry_at = now + timedelta(seconds=profile["cooldown_seconds"])
            _put(
                conn,
                analysis_key,
                {
                    "status": "evidence_too_large",
                    "observed_at": now.isoformat(),
                    "input_generation": packet["input_generation"],
                    "bundle_bytes": packet_bytes,
                    "byte_limit": _MAX_BUNDLE,
                    "evidence_retry_at": retry_at.astimezone(timezone.utc).isoformat(),
                },
                now,
            )
            return None, "evidence_too_large"
        claim = {
            key: packet[key]
            for key in ("bundle_id", "bundle_sha256", "input_generation")
        }
        claim.update(
            job_id=packet["bundle_id"],
            status="reserved",
            target=target,
            claimed_at=now.isoformat(),
            deadline_at=(
                now + timedelta(seconds=profile["timeout_seconds"])
            ).isoformat(),
            evidence_ids=[item["id"] for item in packet["evidence"]],
            cooldown_seconds=profile["cooldown_seconds"],
        )
        _put(
            conn,
            _RUNNER,
            {**runner, "claim": claim, "hourly_calls": [*recent, now.isoformat()]},
            now,
        )
        _put(conn, key, {"claimed_at": now.isoformat()}, now)
        _put(
            conn,
            analysis_key,
            {
                "status": "reserved",
                "claimed_at": now.isoformat(),
                "bundle": packet,
                "bundle_id": claim["bundle_id"],
                "bundle_sha256": claim["bundle_sha256"],
                "input_generation": claim["input_generation"],
            },
            now,
        )
    return claim, "launched"


def poll(config, *, now=None):
    """Start or inspect one diagnosis without waiting on model or process I/O."""
    settings = config.get("supervision", {})
    profile = normalize_diagnosis(settings.get("diagnosis"))
    if settings.get("mode") == "off" or profile is None:
        return _status("disabled")
    observed_monotonic = time.monotonic()
    current = (
        supervision_rules.timestamp(now)
        if now is not None
        else datetime.now(timezone.utc)
    )
    if current is None:
        raise ValueError("invalid diagnosis clock")
    db_path = Path(config["db_path"]).resolve()
    with _JOBS_LOCK:
        if job := _JOBS.get(db_path):
            if job.thread.is_alive():
                return _status("running")
            result = _persist_job(job)
            if result["status"] != "completion_pending":
                del _JOBS[db_path]
            return result
        try:
            with closing(_connect(db_path, readonly=True)) as conn:
                conn.execute("BEGIN")
                if not supervision_store.has_schema(conn):
                    return _status("unavailable", ok=False)
                runner = _checkpoint(conn, _RUNNER)
                if previous := runner.get("claim"):
                    if not previous.get("process_identity"):
                        return _status(
                            "process_attention", previous["target"], ok=False
                        )
                    _JOBS[db_path] = _Job(
                        lambda stop: _recover_previous(db_path, previous, current, stop)
                    )
                    return _status("checking_previous", previous["target"])
                if _provider_wait(runner, current):
                    return {
                        **_status("provider_cooldown"),
                        "retry_at": runner["provider_not_before"],
                    }
                targets = _targets(conn, current)
            rejected = None
            for target in targets:
                with closing(_connect(db_path, readonly=True)) as conn:
                    conn.execute("BEGIN")
                    last = supervision_rules.timestamp(
                        _checkpoint(conn, _cooldown_key(target)).get("claimed_at")
                    )
                    if last is not None and current < last + timedelta(
                        seconds=profile["cooldown_seconds"]
                    ):
                        continue
                    snapshot = _snapshot(conn, target)
                if snapshot is None:
                    continue
                packet = _bundle(snapshot, target, "diagnosis-" + uuid4().hex)
                packet_bytes = len(_json(packet).encode("utf-8"))
                claim, status = _claim(
                    db_path, profile, target, packet, packet_bytes, current
                )
                if status == "evidence_too_large":
                    # Keep scanning the bounded batch even if this target's
                    # cooldown expires before the next resident poll.
                    rejected = _status(status, target, ok=False)
                    continue
                if claim is not None:
                    _JOBS[db_path] = _Job(
                        lambda stop: _run(
                            db_path,
                            claim,
                            packet,
                            profile,
                            current,
                            stop,
                            observed_monotonic=observed_monotonic,
                        )
                    )
                return _status(status, target)
            return rejected or _status("cooldown" if targets else "idle")
        except (OSError, sqlite3.Error, ValueError, RecursionError):
            return _status("unavailable", ok=False)


def shutdown(config):
    """Stop this supervisor's in-flight wrapper, retaining any unresolved claim."""
    db_path = Path(config["db_path"]).resolve()
    with _JOBS_LOCK:
        job = _JOBS.get(db_path)
        if job is None:
            return _status("idle")
        job.stop.set()
    job.thread.join(timeout=5)
    with _JOBS_LOCK:
        if job.thread.is_alive():
            return _status("process_attention", ok=False)
        result = _persist_job(job)
        if result["status"] != "completion_pending" and _JOBS.get(db_path) is job:
            del _JOBS[db_path]
    return result

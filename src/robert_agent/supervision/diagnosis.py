"""Run a bounded, advisory diagnosis outside the business worker queue."""

from robert_agent.foundation.contracts.supervision import SupervisionMode
from robert_agent.foundation.contracts.execution import ModelCommandStatus

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import origins as db_read_origins

from robert_agent.persistence import database as db_database
from robert_agent.persistence import errors as db_errors
from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import supervision_checkpoints as db_read_supervision_checkpoints
from robert_agent.persistence.queries import supervision_incidents as db_read_supervision_incidents
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import worker_phases as db_read_worker_phases

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from robert_agent.context.prompts import render_prompt
from robert_agent.context.supervision_prompts import diagnosis_input
from robert_agent.security import redaction
from robert_agent.supervision import rules as supervision_rules
from robert_agent.supervision import store as supervision_store
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.adapters.processes import model_command
from robert_agent.configuration.supervision import normalize_diagnosis
from robert_agent.worker_adapters import build_worker_launch


_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|token|secret|credentials?|api_key|authorization|cookie)(?:$|_)", re.I
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
_MAX_EXPECTED_PROGRESS = 1000
_MAX_BUNDLE = 49152
_RUNNER = ("diagnostic", "installation", "runner")
_IN_PROGRESS = ("open", "needs_attention", "diagnosing")
_JOBS: dict[str, "_Job"] = {}
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
    conn = db_database.connect(f"{db_path.as_uri()}?mode={mode}", uri=True, timeout=0.05)
    return conn


@contextmanager
def _write(db_path):
    with closing(_connect(db_path)) as conn, conn:
        db_transactions.begin(conn, immediate=True)
        yield conn


def _checkpoint(conn, key):
    return (supervision_store.get_checkpoint(conn, *key) or {}).get("metadata", {})


def _put(conn, key, metadata, now):
    supervision_store.update_checkpoint(conn, *key, now=now.isoformat(), metadata=metadata)


def _target(row, source):
    identifier = row["incident_id"] if source == "incident" else f"candidate:{row['scope_id']}:{row['rule_id']}"
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
    incidents = db_read_supervision_incidents.find_for_targets(conn, cutoff)
    candidates = db_read_supervision_checkpoints.find_for_targets(conn, cutoff)
    return [_target(row, "incident") for row in incidents] + [_target(row, "checkpoint") for row in candidates]


def _read_subject(conn, target):
    if target["source"] == "incident":
        row = db_records.first(db_read_supervision_incidents.find_for_read_subject(conn, target["id"]))
        if not row or row["status"] not in _IN_PROGRESS:
            return None
    else:
        row = db_records.first(
            db_read_supervision_checkpoints.find_for_read_subject(
                conn, target["scope_type"], target["scope_id"], target["rule_id"]
            )
        )
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
    task = db_records.first(db_read_tasks.find_for_task_context(conn, target["scope_id"]))
    if not task:
        return None, None, None
    item = db_records.first(
        db_read_origins.find_for_task_context(conn, task["workstream_id"], task["repo_id"], task["repo_id"])
    )
    attempt = db_records.first(db_read_attempts.find_for_task_context(conn, task["task_id"]))
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
    facts = {"observation": subject, "task": task, "work_item": item, "attempt": attempt}
    if task:
        events = db_read_task_events.find_for_snapshot(conn, task["task_id"], task["repo_id"])
        facts["request_events"] = [dict(row) for row in events]
    if attempt:
        phases = db_read_worker_phases.find_for_snapshot(conn, attempt["attempt_id"])
        facts["worker_phases"] = [dict(row) for row in phases]
    return _safe(facts, -1)


def _bundle(snapshot, target, bundle_id):
    evidence = []
    missing = ["current_external_authorization", "deployed_build", "complete_worker_tool_trace"]
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
    }
    packet["instructions"] = render_prompt(
        diagnosis_input(
            packet,
            classes=_DIAGNOSIS_CLASSES,
            recommendations=_RECOMMENDATIONS,
            max_progress_chars=_MAX_EXPECTED_PROGRESS,
        )
    ).text
    return {**packet, "bundle_sha256": _digest(packet)}


def _status(status, target=None, *, ok=True):
    result = {"ok": ok, "status": status}
    if target:
        result["incident_id" if target["source"] == "incident" else "candidate_id"] = target["id"]
    return result


def _set_claim(db_path, claim, now):
    with _write(db_path) as conn:
        runner = _checkpoint(conn, _RUNNER)
        if (runner.get("claim") or {}).get("job_id") != claim["job_id"]:
            return False
        _put(conn, _RUNNER, {**runner, "claim": claim}, now)
    return True


def _complete(db_path, claim, now, status, diagnosis=None, *, stopped=True, provider_error=None):
    target = claim["target"]
    with _write(db_path) as conn:
        runner = _checkpoint(conn, _RUNNER)
        if (runner.get("claim") or {}).get("job_id") != claim["job_id"]:
            return _status("claim_changed", target, ok=False)
        if diagnosis is not None and _digest(_snapshot(conn, target)) != claim["input_generation"]:
            status, diagnosis = "stale_evidence", None
        record = _checkpoint(conn, _analysis_key(target))
        if record.get("bundle_id") != claim["bundle_id"]:
            return _status("claim_changed", target, ok=False)
        record.update(status=status, observed_at=now.isoformat())
        if diagnosis is not None:
            record["diagnosis"] = diagnosis
        if provider_error is not None:
            delay = max(claim["cooldown_seconds"], provider_error.get("retry_after_seconds", 0))
            retry_at = (now + timedelta(seconds=delay)).isoformat()
            record["provider_error"] = {**provider_error, "retry_at": retry_at}
            runner["provider_not_before"] = retry_at
        _put(conn, _analysis_key(target), record, now)
        _put(conn, _RUNNER, {**runner, "claim": None if stopped else {**claim, "status": "attention"}}, now)
    return _status(status, target, ok=status in {"succeeded", "interrupted", "stopped", "stale_evidence"})


def _validate_output(raw, claim):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate JSON key")
        return result

    output = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(output, dict) or set(output) not in (_OUTPUT_FIELDS, {"bundle_id", "bundle_sha256", "error"}):
        raise ValueError("wrong diagnosis fields")
    if output["bundle_id"] != claim["bundle_id"] or output["bundle_sha256"] != claim["bundle_sha256"]:
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
    for key, values in (("diagnosis_class", _DIAGNOSIS_CLASSES), ("recommended_action", _RECOMMENDATIONS)):
        if not isinstance(output[key], str) or output[key] not in values:
            raise ValueError("unsupported diagnosis class or recommendation")
    ids = output["evidence_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= len(claim["evidence_ids"])
        or any(not isinstance(identifier, str) or identifier not in claim["evidence_ids"] for identifier in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("diagnosis references unknown or duplicate evidence")
    expected = output["expected_progress"]
    if not isinstance(expected, str) or not expected.strip() or len(expected) > _MAX_EXPECTED_PROGRESS:
        raise ValueError("expected progress is missing or too large")
    safe = redaction.redact_text(expected)
    if not safe["ok"]:
        raise ValueError("diagnosis contains sensitive text")
    safe["text"].encode("utf-8")  # JSON escapes can still decode to invalid Unicode.
    return {**output, "expected_progress": safe["text"]}


def _run(db_path, claim, packet, profile, now, stop, *, observed_monotonic):
    started = time.monotonic()
    status, diagnosis, proc, stopped = ModelCommandStatus.STOPPED.value, None, None, True
    provider_error = None
    try:
        directory = tempfile.TemporaryDirectory(prefix="robert-diagnosis-", ignore_cleanup_errors=True)
    except OSError:
        return _PendingCompletion(db_path, claim, now, ModelCommandStatus.LAUNCH_FAILED.value, None, True)
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
            return _PendingCompletion(db_path, claim, now, ModelCommandStatus.LAUNCH_FAILED.value, None, True)
        environment = {name: os.environ[name] for name in launch.environment_allowlist if name in os.environ}
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
                if identity := model_command.stable_identity(proc, stop):
                    if identity["process_group_id"] != proc.pid:
                        raise ValueError("diagnosis process group is not isolated")
                    claim = {**claim, "process_identity": identity, "status": "running"}
                    if not _set_claim(db_path, claim, now):
                        raise ValueError("diagnosis claim changed during spawn")
                status, output = model_command.read_output(proc, started + launch.timeout_seconds, stop)
                if status in {ModelCommandStatus.READ.value, ModelCommandStatus.EXITED.value} and output:
                    try:
                        validated = _validate_output(output, claim)
                        if "error" in validated:
                            status, provider_error = "provider_rate_limited", validated["error"]
                        elif status == ModelCommandStatus.READ.value:
                            status, diagnosis = "succeeded", validated
                    except (ValueError, RecursionError):
                        status = ModelCommandStatus.INVALID_OUTPUT.value
                elif status == ModelCommandStatus.READ.value:
                    status = ModelCommandStatus.INVALID_OUTPUT.value
            except (OSError, db_errors.Error, ValueError, worker_identity.IdentityReadError):
                status = (
                    ModelCommandStatus.LAUNCH_FAILED.value
                    if proc is None
                    else ModelCommandStatus.PROCESS_ATTENTION.value
                )
            finally:
                if proc is not None:
                    proc.stdout.close()
                    try:
                        stopped = proc.poll() is not None and not worker_identity.live_process_group(proc.pid)
                    except (OSError, subprocess.TimeoutExpired):
                        stopped = False
                    if not stopped:
                        stopped = worker_identity.cleanup_new_process(proc, wait_timeout=0.5)
            if not stopped:
                status, diagnosis, provider_error = ModelCommandStatus.PROCESS_ATTENTION.value, None, None
    finished = now + timedelta(seconds=time.monotonic() - observed_monotonic)
    return _PendingCompletion(db_path, claim, finished, status, diagnosis, stopped, provider_error=provider_error)


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
        terminated = worker_identity.terminate_recorded_identity(identity, timeout_seconds=0.5)
        if terminated["status"] in {"terminated", "already_dead"}:
            state = "dead"
    if state == "dead" and not worker_identity.live_process_group(pgid):
        return _complete(db_path, claim, now, "timed_out" if timed_out else "interrupted")
    return _status("process_attention", target, ok=False)


class _PendingCompletion:
    """Keep an already reaped outcome until its durable write succeeds."""

    def __init__(self, db_path, claim, now, status, diagnosis, stopped, *, provider_error=None):
        self.provider_error = provider_error
        self.arguments = (db_path, claim, now, status, diagnosis)
        self.stopped = stopped

    def persist(self):
        return _complete(*self.arguments, stopped=self.stopped, provider_error=self.provider_error)


def _persist_job(job):
    if isinstance(job.result, _PendingCompletion):
        try:
            job.result = job.result.persist()
        except (OSError, db_errors.Error):
            return _status("completion_pending", ok=False)
    return job.result or _status("unavailable", ok=False)


class _Job:
    def __init__(self, callback):
        self.stop = threading.Event()
        self.result = None

        def execute():
            try:
                self.result = callback(self.stop)
            except (OSError, db_errors.Error, ValueError, subprocess.SubprocessError):
                self.result = _status("unavailable", ok=False)

        self.thread = threading.Thread(target=execute, name="robert-diagnosis", daemon=True)
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
            if (parsed := supervision_rules.timestamp(stamp)) is not None and parsed > hour_ago
        ]
        if len(recent) >= profile["max_calls_per_hour"]:
            return None, "rate_limited"
        key = _cooldown_key(target)
        last = supervision_rules.timestamp(_checkpoint(conn, key).get("claimed_at"))
        if last is not None and now < last + timedelta(seconds=profile["cooldown_seconds"]):
            return None, "cooldown"
        analysis_key = _analysis_key(target)
        retry_at = supervision_rules.timestamp(_checkpoint(conn, analysis_key).get("evidence_retry_at"))
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
        claim = {key: packet[key] for key in ("bundle_id", "bundle_sha256", "input_generation")}
        claim.update(
            job_id=packet["bundle_id"],
            status="reserved",
            target=target,
            claimed_at=now.isoformat(),
            deadline_at=(now + timedelta(seconds=profile["timeout_seconds"])).isoformat(),
            evidence_ids=[item["id"] for item in packet["evidence"]],
            cooldown_seconds=profile["cooldown_seconds"],
        )
        _put(conn, _RUNNER, {**runner, "claim": claim, "hourly_calls": [*recent, now.isoformat()]}, now)
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
    if settings.get("mode") == SupervisionMode.OFF.value or profile is None:
        return _status("disabled")
    observed_monotonic = time.monotonic()
    current = supervision_rules.timestamp(now) if now is not None else datetime.now(timezone.utc)
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
                db_transactions.begin(conn, immediate=False)
                if not supervision_store.has_schema(conn):
                    return _status("unavailable", ok=False)
                runner = _checkpoint(conn, _RUNNER)
                if previous := runner.get("claim"):
                    if not previous.get("process_identity"):
                        return _status("process_attention", previous["target"], ok=False)
                    _JOBS[db_path] = _Job(lambda stop: _recover_previous(db_path, previous, current, stop))
                    return _status("checking_previous", previous["target"])
                if _provider_wait(runner, current):
                    return {**_status("provider_cooldown"), "retry_at": runner["provider_not_before"]}
                targets = _targets(conn, current)
            rejected = None
            for target in targets:
                with closing(_connect(db_path, readonly=True)) as conn:
                    db_transactions.begin(conn, immediate=False)
                    last = supervision_rules.timestamp(_checkpoint(conn, _cooldown_key(target)).get("claimed_at"))
                    if last is not None and current < last + timedelta(seconds=profile["cooldown_seconds"]):
                        continue
                    snapshot = _snapshot(conn, target)
                if snapshot is None:
                    continue
                packet = _bundle(snapshot, target, "diagnosis-" + uuid4().hex)
                packet_bytes = len(_json(packet).encode("utf-8"))
                claim, status = _claim(db_path, profile, target, packet, packet_bytes, current)
                if status == "evidence_too_large":
                    # Keep scanning the bounded batch even if this target's
                    # cooldown expires before the next resident poll.
                    rejected = _status(status, target, ok=False)
                    continue
                if claim is not None:
                    _JOBS[db_path] = _Job(
                        lambda stop: _run(
                            db_path, claim, packet, profile, current, stop, observed_monotonic=observed_monotonic
                        )
                    )
                return _status(status, target)
            return rejected or _status("cooldown" if targets else "idle")
        except (OSError, db_errors.Error, ValueError, RecursionError):
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

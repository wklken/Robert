"""Read-only operational task summaries, stage evidence and frozen identities."""

from contextlib import closing
from datetime import datetime, timezone
from collections import defaultdict
import json
import os
from pathlib import Path

from robert_agent.adapters.processes import identity
from robert_agent.adapters.processes.session_history import open_history
from robert_agent.foundation.contracts.execution import AttemptStatus
from robert_agent.foundation.contracts.work import TaskLifecycle, TERMINAL_TASK_LIFECYCLES
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import database as db_database, transactions
from robert_agent.persistence.queries.readviews import task_inspection as db_queries, task_history
from robert_agent.persistence.rounds import pool
from robert_agent.readmodels import execution, usage
from robert_agent.security.text import safe_public_link, safe_text, strip_ansi


ACTIVE_RUN_STATUSES = frozenset(("prepared", "waiting", "starting", "running"))
TIMING_NOTE = (
    "Elapsed is the latest attempt or active stage wall time. Heartbeats and phase reports do not prove model progress. "
    "Historical stages retain their owner. updated_at is not model completion; missing timing is unknown."
)


def _object(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _seconds(start, end):
    start = utc_timestamp(start)
    end = end if isinstance(end, datetime) else utc_timestamp(end)
    return round((end - start).total_seconds(), 3) if start and end and end >= start else None


def _bounds(limit, offset=0):
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1..500 and offset must be non-negative")


def _connect(db_path):
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    conn = db_database.connect(path.as_uri() + "?mode=ro", uri=True, busy_timeout_ms=5000)
    # Hold one read snapshot while joining current owners, phases and receipts.
    transactions.begin(conn)
    return conn


def _page(rows, limit, offset):
    return {"items": rows[:limit], "limit": limit, "offset": offset, "has_more": len(rows) > limit}


def _process(dispatch):
    recorded = dispatch.get("process_identity") or dispatch
    if not isinstance(recorded, dict):
        recorded = {}
    # A PID without its recorded start token/fingerprint is insufficient proof.
    return {"pid": recorded.get("pid"), "status": identity.inspect_recorded_identity(recorded)}


def _attempt(row, now):
    if not row.get("attempt_id"):
        return None
    attempt = {
        "attempt_id": row["attempt_id"],
        "attempt_no": row["attempt_no"],
        "status": row["attempt_status"],
        "started_at": row["started_at"],
        "heartbeat_at": row["heartbeat_at"],
        "finished_at": row["finished_at"],
        "failure_json": row["failure_json"],
        "metadata_json": row["attempt_metadata_json"],
    }
    summary = execution.attempt_summary_from_records(attempt, None)
    terminal = row["attempt_status"] not in {AttemptStatus.RUNNING, AttemptStatus.STALE}
    end = row["finished_at"] if terminal or row["finished_at"] else now
    summary.update(
        elapsed_seconds=_seconds(row["started_at"], end) if row["attempt_status"] != AttemptStatus.PREPARED else None,
        heartbeat_age_seconds=_seconds(row["heartbeat_at"], now),
    )
    return summary


def _run(row, attempt_id, now, receipt=None, *, db_path=None):
    dispatch = _object(row.get("dispatch_json"))
    current = row["owner_attempt_id"] == attempt_id
    activity = usage.summarize_call_activity([receipt or {}])
    events = {}
    segments = (receipt or {}).get("segments")
    for segment in segments.values() if isinstance(segments, dict) else ():
        if not isinstance(segment, dict) or not isinstance(segment.get("events"), dict):
            continue
        for name, fact in segment["events"].items():
            at = utc_timestamp(fact.get("at")) if isinstance(fact, dict) else None
            if name in {"model_completed", "registered", "process_exit_observed"} and at:
                events[name] = max(events.get(name, at), at)
    active = (
        current and row["status"] in ACTIVE_RUN_STATUSES and row.get("owner_attempt_status") in {"running", "stale"}
    )
    return {
        **{
            key: row.get(key)
            for key in (
                "run_id",
                "round_id",
                "kind",
                "owner_attempt_id",
                "owner_task_id",
                "owner_attempt_status",
                "round_task_id",
                "stage",
                "status",
                "attempt_no",
                "started_at",
                "created_at",
                "updated_at",
            )
        },
        "worker_id": safe_text(row.get("worker_id")),
        "model": safe_text(row.get("model")),
        "current_attempt": current,
        "queue_seconds": _seconds(row["created_at"], row["started_at"]),
        "queue_elapsed_seconds": _seconds(row["created_at"], now) if not row["started_at"] and active else None,
        "elapsed_seconds": _seconds(row.get("started_at"), now if active else events.get("process_exit_observed")),
        "timing_events": {name: at.isoformat() for name, at in events.items()},
        "error": safe_text(row.get("error")),
        "slot_reserved": bool(row.get("slot_reserved")),
        "process": _process(dispatch) if active else {"status": "not_observed"},
        "prompt_sha256": safe_text(row.get("prompt_sha256")),
        "model_activity": activity,
        **({"logs": _stage_logs(db_path, row.get("output_dir"), now)} if db_path else {}),
    }


def _stage_logs(db_path, output_dir, now):
    result = {}
    for name in ("stdout.log", "stderr.log"):
        try:
            path = Path(output_dir) / name
            with open_history(Path(db_path).expanduser().absolute().parent, path, trusted_root_alias=True) as fh:
                info = os.fstat(fh.fileno())
                age = round(now.timestamp() - info.st_mtime, 3)
                result[name] = {
                    "missing": False,
                    "bytes": info.st_size,
                    "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    "age_seconds": age if age >= 0 else None,
                }
        except (OSError, TypeError, ValueError, RuntimeError):
            result[name] = {"missing": True, "bytes": None, "age_seconds": None}
    return result


def _summary(row, phase, runs, actions, now):
    metadata = _object(row["metadata_json"])
    wait = metadata.get("wait_state")
    wait = wait if isinstance(wait, dict) else {}
    attention = metadata.get("execution_attention")
    attention = attention if isinstance(attention, dict) else {}
    current = _attempt(row, now)
    publication = [
        {
            "action_id": action["action_id"],
            "audit_status": action["audit_status"],
            "publish_status": action["publish_status"],
        }
        for action in actions
    ]
    active = [_run(run, row["attempt_id"], now) for run in runs if run["round_task_id"] == row["task_id"]]
    reason = safe_text(wait.get("reason_code"))
    if row["lifecycle"] in TERMINAL_TASK_LIFECYCLES:
        reason = "terminal"
    elif row["lifecycle"] == TaskLifecycle.WAITING_FOR_USER:
        reason = "human_input"
    elif attention:
        reason = safe_text(attention.get("reason_code")) or "execution_attention"
    elif any(action["audit_status"] in {"failed", "policy_violation"} for action in actions):
        reason = "audit_attention"
    elif any(
        action["publish_status"] == "not_published" and action["audit_status"] == "accepted" for action in actions
    ):
        reason = "publication_pending"
    elif any(action["audit_status"] == "pending" for action in actions):
        reason = "audit_pending"
    elif not reason:
        reason = "scheduler_pending" if row["lifecycle"] == TaskLifecycle.QUEUED else "worker_or_result_pending"
    return {
        **{
            key: row[key]
            for key in (
                "task_id",
                "workstream_id",
                "public_ref",
                "repo",
                "lifecycle",
                "route_id",
                "created_at",
                "updated_at",
            )
        },
        "title": safe_text(row["title"], 240),
        "source_key": safe_text(row["source_key"]),
        "source_url": safe_public_link(row["html_url"]),
        "current_attempt": current,
        "queue_seconds": _seconds(row["created_at"], row["started_at"]),
        "queue_elapsed_seconds": _seconds(row["created_at"], now)
        if not row["started_at"] and row["lifecycle"] not in TERMINAL_TASK_LIFECYCLES
        else None,
        "phase": {
            "phase": phase["phase"],
            "status": safe_text(phase["status"]),
            "summary": safe_text(phase["summary"]),
            "reported_at": phase["created_at"],
            "reported_age_seconds": _seconds(phase["created_at"], now),
        }
        if phase
        else None,
        "active_stages": active,
        "publication": publication,
        "waiting_reason": reason,
        "waiting_since": safe_text(wait.get("since")),
        "attention_reason": safe_text(attention.get("reason_code")),
    }


def _summaries(conn, rows, now):
    attempt_ids = [row["attempt_id"] for row in rows if row["attempt_id"]]
    phases = {row["attempt_id"]: row for row in task_history.phases_for_attempts(conn, attempt_ids, latest_only=True)}
    runs = defaultdict(list)
    for run in db_queries.active_runs(conn, attempt_ids):
        runs[run["owner_attempt_id"]].append(run)
    actions = defaultdict(list)
    for action in task_history.actions_for_tasks(conn, [row["task_id"] for row in rows]):
        actions[action["task_id"]].append(action)
    return [
        _summary(
            dict(row),
            phases.get(row["attempt_id"]),
            runs[row["attempt_id"]],
            actions[row["task_id"]],
            now,
        )
        for row in rows
    ]


def list_tasks(db_path, *, states=(), repo=None, query=None, since=None, latest=False, limit=50, offset=0, now=None):
    _bounds(limit, offset)
    if set(states) - {state.value for state in TaskLifecycle}:
        raise ValueError("state must be a task lifecycle; use robert task list --help")
    if since and not utc_timestamp(since):
        raise ValueError("since must be an ISO timestamp with timezone, e.g. 2026-11-24T00:00:00+08:00")
    now = now or datetime.now(timezone.utc)
    with closing(_connect(db_path)) as conn:
        rows = db_queries.list_tasks(
            conn, states=states, repo=repo, query=query, since=since, latest=latest, limit=limit, offset=offset
        )
        result = _page(_summaries(conn, rows[:limit], now), limit, offset)
        result["has_more"] = len(rows) > limit
    return result | {"checked_at_utc": now.isoformat(), "note": TIMING_NOTE}


def inspect_task(db_path, task_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    with closing(_connect(db_path)) as conn:
        return inspect_task_from_connection(conn, task_id, now=now)


def inspect_task_from_connection(conn, task_id, *, now=None):
    """Join task evidence inside a caller-owned read snapshot."""
    now = now or datetime.now(timezone.utc)
    rows = db_queries.task(conn, task_id)
    if not rows:
        return None
    summary = _summaries(conn, rows, now)[0]
    summary["publication"] = execution.publication_history(conn, task_id)
    if summary["current_attempt"]:
        dispatch = _object(rows[0]["attempt_metadata_json"]).get("dispatch")
        summary["current_attempt"]["process"] = (
            _process(dispatch if isinstance(dispatch, dict) else {})
            if rows[0]["attempt_status"] in {"running", "stale"}
            else {"status": "not_observed"}
        )
    return summary | {"checked_at_utc": now.isoformat(), "note": TIMING_NOTE}


def control_summary(db_path):
    with closing(_connect(db_path)) as conn:
        return {"lifecycle_counts": db_queries.lifecycle_counts(conn), "model_pool": pool.status(conn)}


def task_stages(db_path, task_id, *, history=False, limit=50, offset=0, now=None):
    _bounds(limit, offset)
    now = now or datetime.now(timezone.utc)
    with closing(_connect(db_path)) as conn:
        tasks = db_queries.task(conn, task_id)
        if not tasks:
            return None
        attempt_id = tasks[0]["attempt_id"]
        rows = db_queries.stages(
            conn, task_id, attempt_id=None if history else attempt_id or "", limit=limit, offset=offset
        )
        receipts = {
            row["call_id"]: _object(row["usage_json"])
            for row in db_queries.usage_for_runs(conn, [row["run_id"] for row in rows[:limit]])
        }
        result = _page(
            [_run(dict(row), attempt_id, now, receipts.get(row["run_id"]), db_path=db_path) for row in rows[:limit]],
            limit,
            offset,
        )
        result["has_more"] = len(rows) > limit
    return result | {
        "task_id": task_id,
        "current_attempt_id": attempt_id,
        "history": history,
        "checked_at_utc": now.isoformat(),
        "note": TIMING_NOTE,
    }


def _manifest(value):
    manifest = _object(value)
    result = {
        key: safe_text(manifest.get(key))
        for key in (
            "head_sha",
            "base_sha",
            "base_ref",
            "diff_sha256",
            "snapshot_sha256",
            "manifest_sha256",
            "prompt_sha256",
        )
        if isinstance(manifest.get(key), str)
    }
    for name in ("files", "source_hashes"):
        hashes = manifest.get(name)
        if isinstance(hashes, dict):
            result[name] = [
                {"name": safe_text(Path(key).name), "path": safe_text(key), "sha256": safe_text(value)}
                for key, value in sorted(hashes.items())[:100]
                if isinstance(key, str) and isinstance(value, str)
            ]
            result[name + "_truncated"] = len(hashes) > 100
    return result


def task_snapshots(db_path, task_id, *, limit=50, offset=0):
    _bounds(limit, offset)
    with closing(_connect(db_path)) as conn:
        if not db_queries.task(conn, task_id):
            return None
        rows = db_queries.snapshots(conn, task_id, limit=limit, offset=offset)
        result = _page(
            [
                {
                    "snapshot_id": row["snapshot_id"],
                    "round_id": row["round_id"],
                    "kind": "pre_pr_review",
                    "round_task_id": row["round_task_id"],
                    "current": row["snapshot_id"] == row["current_snapshot_id"],
                    "created_at": row["created_at"],
                    **_manifest(row["manifest_json"]),
                    "head_sha": row["head_sha"],
                    "base_sha": row["base_sha"],
                }
                for row in rows
            ],
            limit,
            offset,
        )
        result["round_manifests"] = [
            {
                "round_id": row["round_id"],
                "kind": row["kind"],
                "created_at": row["created_at"],
                **_manifest(row["manifest_json"]),
            }
            for row in db_queries.manifests(conn, task_id)
        ]
    return result | {
        "task_id": task_id,
        "note": "Recorded identities only; filesystem or current Git HEAD is not revalidated.",
    }


def task_logs(db_path, task_id, *, max_bytes=65536, lines=40):
    if not 1 <= max_bytes <= 65536 or not 1 <= lines <= 500:
        raise ValueError("max-bytes must be 1..65536 and lines must be 1..500")
    with closing(_connect(db_path)) as conn:
        rows = db_queries.task(conn, task_id)
        if not rows:
            return None
        attempt_id = rows[0]["attempt_id"]
        artifacts = db_queries.stderr_artifacts(conn, task_id, attempt_id) if attempt_id else []
    items = []
    for artifact in artifacts:
        preview = _stderr_preview(db_path, artifact["path"], max_bytes)
        content = preview["content"] if preview else ""
        selected = content.splitlines(keepends=True)[-lines:]
        items.append(
            {
                "artifact_id": artifact["artifact_id"],
                "attempt_id": attempt_id,
                "artifact_type": artifact["artifact_type"],
                "path": safe_text(artifact["path"]),
                "missing": preview is None,
                "truncated": bool(preview and preview["truncated"]) or len(selected) < len(content.splitlines()),
                "content": safe_text(strip_ansi("".join(selected)), max_bytes),
            }
        )
    return {
        "task_id": task_id,
        "current_attempt_id": attempt_id,
        "items": items,
        "max_bytes": max_bytes,
        "lines": lines,
        "note": "Registered stderr for the latest attempt only. Missing evidence does not prove a stalled process.",
    }


def _stderr_preview(db_path, path, max_bytes):
    try:
        with open_history(Path(db_path).expanduser().absolute().parent, path, trusted_root_alias=True) as fh:
            info = os.fstat(fh.fileno())
            if info.st_size > max_bytes:
                fh.seek(-max_bytes, 2)
            return {
                "content": fh.read(max_bytes).decode("utf-8", errors="replace"),
                "truncated": info.st_size > max_bytes,
            }
    except (OSError, ValueError, RuntimeError):
        return None

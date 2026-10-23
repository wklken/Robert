"""Safe, bounded execution information for the operator board."""
import json
from pathlib import Path

from robert_agent.redaction import redact_and_truncate


def safe_text(value, limit=1000):
    if not isinstance(value, str):
        return ""
    result = redact_and_truncate(value, limit)
    return result.get("text", "Sensitive output hidden; inspect the local worker log.")


def publication_history(conn, task_id):
    actions = []
    for row in conn.execute(
        "SELECT action_id, action_type, audit_status, publish_status, created_at, metadata_json "
        "FROM github_actions WHERE task_id=? ORDER BY created_at, action_id",
        (task_id,),
    ):
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
            publication = metadata.get("publish", {}) if isinstance(metadata, dict) else {}
        except (TypeError, ValueError):
            publication = {}
        if not isinstance(publication, dict):
            publication = {}
        # Only expose the receipt fields; bodies, responses and worktree paths
        # are not part of the board's public read model.
        actions.append({
            "action_id": row["action_id"],
            "action_type": safe_text(row["action_type"]),
            "audit_status": safe_text(row["audit_status"]),
            "status": safe_text(row["publish_status"] if row["publish_status"] in {"published", "skipped"} else publication.get("status") or row["publish_status"]),
            "created_at": row["created_at"],
            "published_at": safe_text(publication.get("published_at")),
            "safe_error": safe_text(publication.get("safe_error")),
            "reason_code": safe_text(publication.get("reason_code")),
            "attempt_count": publication.get("attempt_count", 0),
            "max_attempts": publication.get("max_attempts"),
            "next_retry_at": safe_text(publication.get("next_retry_at")),
            "required_action": safe_text(publication.get("required_action")),
        })
    return actions


def execution_summary(conn, task_id):
    if not task_id:
        return None
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not attempt:
        return None
    return attempt_summary(conn, attempt)


def attempt_summary(conn, attempt):
    phase = conn.execute(
        """SELECT phase, status, summary, next_step, created_at FROM worker_phases
        WHERE attempt_id = ? ORDER BY created_at DESC, phase_id DESC LIMIT 1""",
        (attempt["attempt_id"],),
    ).fetchone()
    try:
        failure = json.loads(attempt["failure_json"] or "{}")
    except (TypeError, ValueError):
        failure = {}
    if not isinstance(failure, dict):
        failure = {}
    try:
        metadata = json.loads(attempt["metadata_json"] or "{}")
        termination = metadata.get("termination") or {}
    except (ValueError, AttributeError):
        termination = {}
    cleanup_reason = ""
    if isinstance(termination, dict) and termination.get("status") not in {None, "terminated", "not_found", "already_dead"}:
        cleanup_reason = safe_text(termination.get("reason") or termination.get("status"))
    # Never return raw stderr, environment, metadata or arbitrary failure JSON.
    reason = next((failure[key] for key in ("safe_error", "reason", "original_status", "status", "error_type") if isinstance(failure.get(key), str)), "")
    return {
        "attempt_id": attempt["attempt_id"],
        "attempt_no": attempt["attempt_no"],
        "status": attempt["status"],
        "started_at": attempt["started_at"],
        "heartbeat_at": attempt["heartbeat_at"],
        "finished_at": attempt["finished_at"],
        "phase": phase["phase"] if phase else None,
        "phase_status": safe_text(phase["status"]) if phase else None,
        "summary": safe_text(phase["summary"]) if phase else "",
        "next_step": safe_text(phase["next_step"]) if phase else "",
        "updated_at": phase["created_at"] if phase else attempt["heartbeat_at"],
        "failure": safe_text(reason),
        "cleanup_reason": cleanup_reason,
    }


def attempt_history(conn, attempt):
    return {
        **attempt_summary(conn, attempt),
        "artifacts": [
            {"artifact_id": row["artifact_id"], "artifact_type": row["artifact_type"],
             "bytes": row["bytes"], "available": Path(row["path"]).expanduser().is_file()}
            for row in conn.execute(
                "SELECT artifact_id,artifact_type,path,bytes FROM artifacts WHERE attempt_id=? ORDER BY created_at,artifact_id",
                (attempt["attempt_id"],),
            )
        ],
        "phases": [{"phase": safe_text(row["phase"]), "status": safe_text(row["status"]),
                    "summary": safe_text(row["summary"]), "next_step": safe_text(row["next_step"]),
                    "created_at": row["created_at"]}
                   for row in conn.execute("SELECT * FROM worker_phases WHERE attempt_id=? ORDER BY created_at, phase_id", (attempt["attempt_id"],))],
    }

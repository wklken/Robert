"""Read-only work-item lifecycle facts shared by commands and projections."""

import json
from robert_agent.execution import inspection as supervise
from robert_agent.security.text import safe_text


def related_workstreams(conn, workstream_id):
    return [
        row[0]
        for row in conn.execute(
            "SELECT workstream_id FROM workstreams WHERE workstream_id = ? OR origin_workstream_id = ?",
            (workstream_id, workstream_id),
        )
    ]


def lifecycle_facts(conn, item, *, controls=None):
    streams = related_workstreams(conn, item["workstream_id"])
    placeholders = ",".join("?" for _ in streams) or "NULL"
    cursor = conn.execute(
        f"""SELECT a.attempt_id, a.task_id, a.status, a.metadata_json, gs.state AS source_state FROM attempts a
        JOIN tasks t ON t.task_id=a.task_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        LEFT JOIN github_sources gs ON gs.source_id=w.primary_source_id WHERE t.workstream_id IN ({placeholders})
        AND {supervise.occupied_attempt_sql()}""",
        streams,
    )
    names = [column[0] for column in cursor.description]
    attempts = [dict(zip(names, row)) for row in cursor]
    cleanup = []
    for attempt in attempts:
        try:
            metadata = json.loads(attempt["metadata_json"] or "{}")
            termination = metadata.get("termination") or {}
            supervision = metadata.get("supervise") or {}
            if isinstance(supervision, dict) and supervision.get("status") == "worker_identity_attention":
                cleanup.append(
                    safe_text(
                        supervision.get("reason")
                        or "Automatic recovery stopped: verify worker identity or recovery quota, then cancel this execution."
                    )
                )
        except (ValueError, AttributeError):
            termination = {}
        if isinstance(termination, dict) and termination.get("status") not in {
            None,
            "terminated",
            "not_found",
            "already_dead",
        }:
            cleanup.append(safe_text(termination.get("reason") or termination.get("status")))
    cursor = conn.execute(
        f"""SELECT ga.*, pi.status AS intent_status FROM github_actions ga JOIN tasks t ON t.task_id=ga.task_id
        LEFT JOIN publication_intents pi ON pi.action_id=ga.action_id
        WHERE t.workstream_id IN ({placeholders}) AND ga.publish_status='not_published'
        AND ga.audit_status IN ('pending','accepted') ORDER BY ga.created_at, ga.action_id""",
        streams,
    )
    names = [column[0] for column in cursor.description]
    actions = [dict(zip(names, row)) for row in cursor]
    failed = []
    retry_blocked = False
    for action in actions:
        try:
            metadata = json.loads(action["metadata_json"] or "{}")
            publication = metadata.get("publish") or {}
        except (ValueError, AttributeError):
            publication = {}
        if action["intent_status"] == "attention" or (
            isinstance(publication, dict) and publication.get("retryable") is False
        ):
            retry_blocked = True
        if action["intent_status"] == "attention" and not publication:
            publication = {
                "status": "publish_failed",
                "safe_error": "Publication reconciliation requires operator attention.",
            }
        if isinstance(publication, dict) and publication.get("status") == "publish_failed":
            failed.append(
                {
                    "action_id": action["action_id"],
                    "type": action["action_type"],
                    "reason": safe_text(publication.get("safe_error"))
                    + " "
                    + safe_text(
                        publication.get("required_action")
                        or (("Next retry: " + publication["next_retry_at"]) if publication.get("next_retry_at") else "")
                    ),
                    "failed_at": publication.get("failed_at"),
                    **{
                        key: publication.get(key)
                        for key in (
                            "reason_code",
                            "attempt_count",
                            "max_attempts",
                            "retryable",
                            "next_retry_at",
                            "required_action",
                        )
                    },
                }
            )
    attention = []
    for (encoded,) in conn.execute(
        f"SELECT metadata_json FROM tasks WHERE workstream_id IN ({placeholders})",
        streams,
    ):
        stopped = json.loads(encoded or "{}").get("execution_attention")
        if isinstance(stopped, dict):
            attention.append(safe_text(stopped.get("reason_code") or "Execution requires operator attention."))
    from robert_agent.adapters.sqlite import execution_control as execution_store

    subjects = set()
    for task_id, episode_id in conn.execute(
        f"SELECT task_id,json_extract(metadata_json,'$.remediation_episode_id') FROM tasks WHERE workstream_id IN ({placeholders})",
        streams,
    ):
        subjects.add(("task", task_id))
        if episode_id:
            subjects.add(("remediation_episode", episode_id))
    if controls is None:
        controls = execution_store.read_controls(conn, subjects)
    for key in sorted(subjects):
        binding = controls.get(key)
        if binding and (
            binding["binding_status"] == "attention"
            or (binding.get("owner_engine") == "dbos" and binding["binding_status"] != "terminal")
        ):
            metadata = json.loads(binding["metadata_json"] or "{}")
            attention.append(
                safe_text(metadata.get("reason_code") or "Execution requires operator attention or offline migration.")
            )
    source = conn.execute(
        "SELECT state FROM github_sources WHERE source_id=?",
        (item["origin_source_id"],),
    ).fetchone()
    pending = conn.execute(
        """SELECT 1 FROM wakeups WHERE work_item_id=? AND status='pending'
        AND json_extract(metadata_json, '$.kind')='publication_retry' LIMIT 1""",
        (item["work_item_id"],),
    ).fetchone()
    approval_task = conn.execute(
        """SELECT t.task_id FROM workstreams w JOIN tasks t ON t.task_id=w.active_task_id
        AND t.workstream_id=w.workstream_id WHERE w.workstream_id=? AND w.lifecycle='active'
        AND t.lifecycle IN ('detected','authorized','classified','queued','running')""",
        (item["workstream_id"],),
    ).fetchone()
    return {
        "streams": streams,
        "running": attempts,
        "actions": actions,
        "publish_failures": failed,
        "approval_task_id": approval_task[0] if approval_task else None,
        "workflow_attention": attention,
        "publication_retry_pending": bool(pending),
        "publication_retry_blocked": retry_blocked,
        "cleanup_pending": cleanup,
        "source_closed": bool(source and (source[0] or "").lower() == "closed")
        or any((a["source_state"] or "").lower() == "closed" for a in attempts),
    }


def current_task(conn, streams):
    placeholders = ",".join("?" for _ in streams) or "NULL"
    return conn.execute(
        f"""SELECT t.* FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE t.workstream_id IN ({placeholders})
        ORDER BY EXISTS(SELECT 1 FROM attempts a WHERE a.task_id=t.task_id AND a.status IN ('running','stale')) DESC,
          (t.task_id=w.active_task_id) DESC, t.created_at DESC, t.task_id DESC LIMIT 1""",
        streams,
    ).fetchone()

"""Durable request identity and admission evidence written by business producers."""

from dataclasses import dataclass
import hashlib
import json

from robert_agent.supervision import store as supervision_store


LINEAGE_FIELD = "supervision_lineage_key"
_ADMISSION_RULE = "admission"
_OWNED_DISPOSITIONS = {"admitted_task", "queued_native_round", "linked_pending"}


@dataclass(frozen=True)
class Admission:
    scope_type: str
    scope_id: str
    generation: str


def _digest(*parts):
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def github_key(event):
    """Identify the actual request, including a fresh follow-up's own event."""
    return "github:" + _digest(event["source_key"], event["event_fingerprint"])


def operator_key(work_item_id, command_key):
    return "operator:" + _digest(work_item_id, command_key)


def episode_key(episode_id):
    return "episode:" + episode_id


def get_task_lineage(conn, task_id):
    if not task_id:
        return None
    row = conn.execute(
        "SELECT json_extract(metadata_json,'$.supervision_lineage_key') FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def is_driving_github_input(event):
    return event.get("authorization_status") in {
        "authorized_trigger",
        "authorized_system_trigger",
    } or (
        event.get("authorization_status")
        in {"accepted_context", "accepted_review_participant"}
        and event.get("drives_execution") is True
    )


def github_admission(event, event_id):
    return Admission(
        "github_event",
        event_id,
        _digest(
            event["source_key"],
            event["event_fingerprint"],
            event.get("actor_login"),
            event.get("event_type"),
            event.get("body_observed"),
            event.get("body"),
        ),
    )


def _record_received(conn, admission, repo_id, source_id, lineage_key, now):
    metadata = json.dumps(
        {
            "repo_id": repo_id,
            "source_id": source_id,
            "lineage_key": lineage_key,
            "received_at": now,
            "input_generation": admission.generation,
        },
        sort_keys=True,
    )
    conn.execute(
        """INSERT INTO supervision_checkpoints(scope_type,scope_id,rule_id,lineage_key,updated_at,metadata_json)
        VALUES (?,?,?,?,?,?) ON CONFLICT(scope_type,scope_id,rule_id) DO UPDATE SET
        lineage_key=excluded.lineage_key,updated_at=excluded.updated_at,metadata_json=excluded.metadata_json
        WHERE json_extract(supervision_checkpoints.metadata_json,'$.input_generation')
           IS NOT json_extract(excluded.metadata_json,'$.input_generation')""",
        (
            admission.scope_type,
            admission.scope_id,
            _ADMISSION_RULE,
            lineage_key or "",
            now,
            metadata,
        ),
    )


def record_github_received(conn, event, event_id, repo_id, source_id, now):
    """Persist receipt while the transaction still owns the GitHub event write."""
    admission = github_admission(event, event_id)
    _record_received(conn, admission, repo_id, source_id, github_key(event), now)
    return admission


def record_disposition(
    conn,
    admission,
    *,
    disposition,
    now,
    target_type=None,
    target_id=None,
    lineage_key=None,
    consumer=None,
    due_at=None,
    reason_code=None,
):
    """Attach an effect to its received generation in the same transaction."""
    if admission is None:
        return False
    if disposition in _OWNED_DISPOSITIONS and (not target_type or not target_id):
        raise ValueError("owned admission disposition requires a target")
    if disposition == "deferred" and (not consumer or not due_at):
        raise ValueError("deferred admission requires a consumer and due time")
    patch = json.dumps(
        {
            "disposition": disposition,
            "target_type": target_type,
            "target_id": target_id,
            "consumer": consumer,
            "due_at": due_at,
            "reason_code": reason_code,
            **(
                {"lineage_key": lineage_key}
                if disposition in _OWNED_DISPOSITIONS
                else {}
            ),
        },
        sort_keys=True,
    )
    # Replayed duplicate and source decisions cannot erase established ownership.
    # Only a pending disposition may advance to a different kind of owner.
    result = conn.execute(
        """UPDATE supervision_checkpoints SET metadata_json=json_patch(metadata_json,?),updated_at=?,
          lineage_key=COALESCE(?,lineage_key)
        WHERE scope_type=? AND scope_id=? AND rule_id=? AND json_extract(metadata_json,'$.input_generation')=?
          AND COALESCE(json_extract(metadata_json,'$.disposition'),'') NOT IN ('admitted_task','queued_native_round')
          AND (COALESCE(json_extract(metadata_json,'$.disposition'),'')!='linked_pending' OR ?='admitted_task')
          AND metadata_json != json_patch(metadata_json,?)""",
        (
            patch,
            now,
            (lineage_key or "") if disposition in _OWNED_DISPOSITIONS else None,
            admission.scope_type,
            admission.scope_id,
            _ADMISSION_RULE,
            admission.generation,
            disposition,
            patch,
        ),
    )
    return result.rowcount == 1


def record_operator_admission(conn, item, command_event, task_id, now):
    """Operator commands create the durable consumer in their intake transaction."""
    if not task_id or not supervision_store.has_schema(conn):
        return
    event_id = command_event["event_id"]
    admission = Admission("work_item_event", event_id, event_id)
    _record_received(
        conn,
        admission,
        item["repo_id"],
        item["origin_source_id"],
        get_task_lineage(conn, task_id),
        now,
    )
    record_task_disposition(conn, admission, task_id, now)


def record_task_disposition(conn, admission, task_id, now, *, pending=False):
    if admission is None:
        return
    record_disposition(
        conn,
        admission,
        disposition="linked_pending" if pending else "admitted_task",
        now=now,
        target_type="task",
        target_id=task_id,
        lineage_key=get_task_lineage(conn, task_id),
    )

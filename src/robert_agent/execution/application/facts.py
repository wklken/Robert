"""Apply workflow invalidation facts and stop fenced attempts."""

from robert_agent.execution import operations as execution_operations
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.work import events as workflow_events
from robert_agent.work import queries as work_queries


def _workflow_event_subject(conn, event):
    """Resolve an event to an existing exact workflow binding only."""

    metadata = event.get("metadata") if isinstance(event, dict) else {}
    remediation_id = (
        metadata.get("remediation", {}).get("episode_id")
        if isinstance(metadata, dict) and isinstance(metadata.get("remediation"), dict)
        else None
    )
    if remediation_id:
        subject_type, subject_id = "remediation_episode", str(remediation_id)
    else:
        task_id = event.get("task_id") or work_queries.active_task_id(
            conn, event.get("workstream_id")
        )
        if not task_id:
            return None
        subject_type, subject_id = "task", str(task_id)
    binding = execution_store.get_binding(conn, subject_type, subject_id)
    if not binding:
        return None
    return subject_type, subject_id, binding


def apply_workflow_event_fact(conn, event, run_now):
    """Classify and apply one event when its existing binding is known."""

    subject = _workflow_event_subject(conn, event)
    if not subject:
        return None
    subject_type, subject_id, binding = subject
    fact = {
        **event,
        "fact_id": event.get("fact_id") or event.get("event_fingerprint"),
        "subject_type": subject_type,
        "subject_id": subject_id,
    }
    try:
        result = execution_operations.apply_classified_fact(
            conn,
            subject_type,
            subject_id,
            fact,
            now=run_now,
        )
    except ValueError as exc:
        # A claimed invalidation without proof is fail-closed.  Do not invent
        # evidence or a new binding; the existing owner requires attention.
        reason = str(exc)
        if "invalidation" not in reason and not any(
            key in fact
            for key in (
                "invalidation_reason",
                "old_head_sha",
                "new_head_sha",
                "old_base_sha",
                "new_base_sha",
                "source_branch_exists",
                "old_permission",
                "new_permission",
            )
        ):
            raise
        if binding.get("binding_status") == "active":
            execution_store.mark_binding_attention(
                conn,
                subject_type,
                subject_id,
                run_now,
            )
        return {
            "classification": workflow_events.INVALIDATING,
            "fact_id": fact["fact_id"],
            "binding_status": "attention",
            "attempt_id": execution_operations.bound_attempt_for_subject(
                conn, subject_type, subject_id
            ),
            "validation_error": reason,
        }
    result["subject_type"] = subject_type
    result["subject_id"] = subject_id
    return result


def terminate_invalidated_attempts(db_path, attempt_ids):
    """Terminate only exact Attempts after the classifier transaction commits."""

    outcomes = []
    for attempt_id in dict.fromkeys(attempt_ids):
        outcomes.append(
            (attempt_id, execution_operations.terminate_worker(db_path, attempt_id))
        )
    return outcomes

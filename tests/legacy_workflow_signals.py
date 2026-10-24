"""Historical DBOS-format fixture helpers; not part of Robert runtime."""


from tests import legacy_workflow_store as workflow_store


DBOS_TOPIC = "subject-events"
_MESSAGE_KEYS = {"signal_id", "topic", "subject_type", "subject_id", "fact_id"}


def _required_id(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"workflow signal {name} must be a non-empty string")
    return value


def record_subject_signal(conn, binding, topic, dedupe_key, payload, now):
    """Insert one ID-only subject signal using the caller's transaction."""

    if not isinstance(binding, dict):
        raise ValueError("workflow binding must be a mapping")
    if binding.get("owner_engine") != "dbos":
        raise ValueError("workflow signal requires a DBOS-owned binding")
    if binding.get("binding_status", "active") != "active":
        raise ValueError("workflow signal requires an active binding")
    workflow_id = _required_id(binding.get("workflow_id"), "workflow_id")
    subject_type = _required_id(binding.get("subject_type"), "subject_type")
    subject_id = _required_id(binding.get("subject_id"), "subject_id")
    topic = _required_id(topic, "topic")
    dedupe_key = _required_id(dedupe_key, "dedupe_key")
    if not isinstance(payload, dict) or set(payload) != {"fact_id"}:
        raise ValueError("workflow signal payload must contain only fact_id")
    fact_id = _required_id(payload.get("fact_id"), "fact_id")
    signal_id = workflow_store.signal_id_for(workflow_id, topic, dedupe_key)
    message = {
        "signal_id": signal_id,
        "topic": topic,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "fact_id": fact_id,
    }
    return workflow_store.enqueue_signal(
        conn,
        workflow_id,
        topic,
        dedupe_key,
        message,
        now,
    )

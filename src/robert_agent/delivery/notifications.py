"""delivery notifications responsibilities."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.stores import notifications as db_write_notifications

from robert_agent.delivery import values as publication_values


def notification_metadata(action, result):
    return {
        "action_id": action["action_id"],
        "action_type": action["action_type"],
        "safe_error": result.get("safe_error", ""),
        "command": result.get("command", []),
        **{
            key: result.get(key)
            for key in (
                "reason_code",
                "error_class",
                "attempt_count",
                "max_attempts",
                "next_retry_at",
                "required_action",
                "retryable",
            )
            if key in result
        },
    }


def existing_action_notification(conn, action, notification_type):
    for row in db_read_notifications.find_for_existing_action_notification(conn, action["task_id"], notification_type):
        metadata = publication_values.decode_json(row["metadata_json"])
        if metadata.get("action_id") == action["action_id"]:
            return row["notification_id"]
    return None


def insert_notification(conn, action, notification_type, result, now):
    metadata = notification_metadata(action, result)
    notification_id = existing_action_notification(conn, action, notification_type)
    if notification_id:
        existing_metadata = db_records.first(
            db_read_notifications.find_metadata_json_by_notification_id(conn, notification_id)
        )
        previous = publication_values.decode_json(existing_metadata[0])
        if previous.get("delivery") and all(
            previous.get(key) == metadata.get(key)
            for key in ("reason_code", "error_class", "required_action", "retryable")
        ):
            metadata["delivery"] = previous["delivery"]
        db_write_notifications.insert_notification_2(conn, publication_values.encode_json(metadata), notification_id)
        return
    db_write_notifications.insert_notification(
        conn,
        publication_values.id("notification"),
        action["task_id"],
        notification_type,
        now,
        publication_values.encode_json(metadata),
    )


def resolve_action_notification(conn, action, notification_type, result, now):
    notification_id = existing_action_notification(conn, action, notification_type)
    if not notification_id:
        return
    row = db_records.first(db_read_notifications.find_metadata_json_by_notification_id(conn, notification_id))
    metadata = publication_values.decode_json(row[0] if row else "{}")
    metadata["resolved_at"] = now
    metadata["resolved_publish_status"] = result.get("status", "")
    metadata["resolved_external_id"] = result.get("external_id", "")
    metadata["resolved_target_url"] = result.get("target_url", "")
    db_write_notifications.update_for_resolve_action_notification(
        conn, publication_values.encode_json(metadata), notification_id
    )

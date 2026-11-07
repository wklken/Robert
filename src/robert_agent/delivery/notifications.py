"""delivery notifications responsibilities."""

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
    for row in conn.execute(
        """
        SELECT notification_id, metadata_json
        FROM notifications
        WHERE task_id = ?
          AND notification_type = ?
          AND channel = 'local'
        ORDER BY created_at DESC, notification_id DESC
        """,
        (action["task_id"], notification_type),
    ):
        metadata = publication_values.decode_json(row["metadata_json"])
        if metadata.get("action_id") == action["action_id"]:
            return row["notification_id"]
    return None


def insert_notification(conn, action, notification_type, result, now):
    metadata = notification_metadata(action, result)
    notification_id = existing_action_notification(conn, action, notification_type)
    if notification_id:
        existing_metadata = conn.execute(
            "SELECT metadata_json FROM notifications WHERE notification_id=?",
            (notification_id,),
        ).fetchone()
        previous = publication_values.decode_json(existing_metadata[0])
        if previous.get("delivery") and all(
            previous.get(key) == metadata.get(key)
            for key in ("reason_code", "error_class", "required_action", "retryable")
        ):
            metadata["delivery"] = previous["delivery"]
        conn.execute(
            """
            UPDATE notifications
            SET status = 'recorded',
                created_at = ?,
                metadata_json = ?
            WHERE notification_id = ?
            """,
            (now, publication_values.encode_json(metadata), notification_id),
        )
        return
    conn.execute(
        """
        INSERT INTO notifications(
          notification_id, task_id, notification_type, channel, status, created_at, metadata_json
        )
        VALUES (?, ?, ?, 'local', 'recorded', ?, ?)
        """,
        (
            publication_values.id("notification"),
            action["task_id"],
            notification_type,
            now,
            publication_values.encode_json(metadata),
        ),
    )


def resolve_action_notification(conn, action, notification_type, result, now):
    notification_id = existing_action_notification(conn, action, notification_type)
    if not notification_id:
        return
    row = conn.execute(
        "SELECT metadata_json FROM notifications WHERE notification_id = ?",
        (notification_id,),
    ).fetchone()
    metadata = publication_values.decode_json(row[0] if row else "{}")
    metadata["resolved_at"] = now
    metadata["resolved_publish_status"] = result.get("status", "")
    metadata["resolved_external_id"] = result.get("external_id", "")
    metadata["resolved_target_url"] = result.get("target_url", "")
    conn.execute(
        """
        UPDATE notifications
        SET status = 'resolved',
            created_at = ?,
            metadata_json = ?
        WHERE notification_id = ?
        """,
        (now, publication_values.encode_json(metadata), notification_id),
    )

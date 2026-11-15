"""Persist execution notices without external notification effects."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.stores import notifications as db_write_notifications

import json
from robert_agent.delivery import messages
from uuid import uuid4


def insert(conn, notification_type, status, run_now, metadata=None):
    metadata = metadata or {}
    db_write_notifications.insert_for_insert(
        conn,
        f"notification-{uuid4().hex[:12]}",
        metadata.get("task_id"),
        notification_type,
        status,
        run_now,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )


def update_failure(conn, kind, key, run_now, metadata=None):
    """Keep one notice per unresolved source/repository failure episode."""
    if metadata is None:
        db_write_notifications.update_failure(conn, kind, key)
    elif not db_records.first(db_read_notifications.update_failure(conn, kind, key)):
        insert(conn, kind, "recorded", run_now, {**metadata, "failure_key": key})


def resolve_control_failure(conn, lane):
    db_write_notifications.update_for_resolve_control_failure(conn, lane)


def record_control_stop(conn, daemon_run_id, lane, now, attention):
    db_write_notifications.record_control_stop(
        conn, "notification-control-stop-" + daemon_run_id + "-" + lane, now, json.dumps(attention)
    )


def has_pending_local_delivery(conn, repo_ids, now, config=None):
    access = messages.notification_access(conn, config, delivery=True)
    return bool(
        db_read_notifications.has_pending_local_delivery_filtered(
            conn,
            repo_ids,
            now,
            messaging_repo_ids=access.repo_ids,
            tapd_project_repos=access.tapd_project_repos,
            tapd_projects=access.tapd_projects,
            messaging_settings=config.get("messaging", {}) if config is not None else None,
        )
    )

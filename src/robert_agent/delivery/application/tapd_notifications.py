"""Deliver TAPD ticket notices at least once within the shared retry budget."""

from contextlib import closing
import json

from robert_agent.delivery.application import tapd_comments
from robert_agent.execution import retry as retry_policy
from robert_agent.foundation import tapd_identity, values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.persistence import database as db_database
from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_sources as db_read_github_sources
from robert_agent.persistence.queries import notifications as db_read_notifications
from robert_agent.persistence.stores import notifications as db_write_notifications
from robert_agent.work import tapd_policy

_LABEL_REASONS = {
    "missing": "这张单据没有仓库标签，无法确定在哪个仓库处理。",
    "conflict": "这张单据有多个仓库标签，无法确定在哪个仓库处理。",
}


def notice_body(notification_type, metadata, mention_name):
    """Fixed templates only: a notice never carries execution output or grants authority."""
    if notification_type != tapd_policy.LABEL_NOTICE or metadata.get("reason") not in _LABEL_REASONS:
        return None
    labels = "、".join(metadata.get("labels") or [])
    text = f"{_LABEL_REASONS[metadata['reason']]}请只保留以下标签中的一个，再发一条提及 {mention_name} 的评论：{labels}"
    return f"{tapd_policy.automatic_prefix(mention_name)}\n\n{tapd_policy.neutralize_mentions(text, mention_name)}"


def _ended(conn, metadata, kind):
    row = db_read_github_sources.find_ended_tapd_ticket(
        conn, tapd_identity.SOURCE_TYPES[kind], metadata.get("project_key"), metadata.get("external_id")
    )
    return bool(db_records.first(row))


def _record(db_path, notification_id, status, delivery):
    with closing(db_database.connect(db_path, timeout=5)) as conn, conn:
        db_write_notifications.record_tapd_delivery(
            conn, notification_id, status, json.dumps(delivery, ensure_ascii=False, sort_keys=True)
        )


def _deliver(db_path, publisher, notification_id, notification_type, metadata, now):
    previous = metadata.get("tapd_delivery") or {}
    target = tapd_comments.ticket_target(metadata.get("url"), publisher.settings)
    body = notice_body(notification_type, metadata, publisher.settings["mention_name"]) if target else None
    if body is None:
        _record(db_path, notification_id, "failed", {"status": "failed", "reason_code": "notice_unsupported"})
        return "failed"
    with closing(db_database.connect(db_path, timeout=5)) as conn:
        ended = _ended(conn, metadata, target[1])
    if ended:
        _record(db_path, notification_id, "skipped", {"status": "skipped", "reason_code": "ticket_ended"})
        return "skipped"
    try:
        comment_id = publisher.client().add_comment(*target, body)
    except ControlDeadlineExceeded:
        raise
    except tapd_comments.PROTOCOL_ERRORS as exc:
        failure = retry_policy.failure_state(previous, getattr(exc, "code", type(exc).__name__), now)
        status = "pending" if failure["retryable"] else "failed"
        _record(db_path, notification_id, status, {**failure, "status": status})
        return status
    _record(
        db_path,
        notification_id,
        "delivered",
        {
            "status": "delivered",
            "comment_id": comment_id,
            "delivered_at": now,
            "attempt_count": previous.get("attempt_count", 0) + 1,
        },
    )
    return "delivered"


def deliver_pending(config, *, publisher=None, now=None, limit=20):
    """Duplicates after an unknown write are accepted (R19); there is no publication reconciliation."""
    publisher = publisher or tapd_comments.publisher_for(config)
    if publisher is None:
        return {"enabled": False}
    now = now or values.utc_now()
    with closing(db_database.connect(config["db_path"], timeout=5)) as conn:
        rows = db_read_notifications.find_pending_tapd(conn, now, limit)
    outcomes = {}
    for notification_id, notification_type, metadata_json in rows:
        status = _deliver(
            config["db_path"], publisher, notification_id, notification_type, values.json_object(metadata_json), now
        )
        outcomes[status] = outcomes.get(status, 0) + 1
    return {"enabled": True, **outcomes}

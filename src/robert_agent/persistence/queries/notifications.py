"""Shared queries operations for notifications."""

from robert_agent.persistence.expressions import sql_constant
from robert_agent.persistence.dialects import sqlite

from sqlalchemy import (
    select,
    literal_column,
    bindparam,
    and_,
    or_,
    not_,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements
from robert_agent.persistence.queries.dispatch_requests import task_binding


def find_task_id_and_notification_type_and_status_and_metadata_json_by_notification_id(conn, notification_id):
    query1 = (
        select(
            tables["notifications"].c["task_id"],
            tables["notifications"].c["notification_type"],
            tables["notifications"].c["status"],
            tables["notifications"].c["metadata_json"],
        )
        .select_from(tables["notifications"])
        .where((tables["notifications"].c["notification_id"] == bindparam("p0")))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": notification_id})


def find_metadata_json_by_notification_id(conn, notification_id):
    query1 = (
        select(tables["notifications"].c["metadata_json"])
        .select_from(tables["notifications"])
        .where((tables["notifications"].c["notification_id"] == bindparam("p0")))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": notification_id})


def find_for_existing_action_notification(conn, task_id, notification_type):
    query1 = (
        select(
            tables["notifications"].c["notification_id"],
            tables["notifications"].c["metadata_json"],
        )
        .select_from(tables["notifications"])
        .where(
            and_(
                tables["notifications"].c["task_id"] == bindparam("p0"),
                tables["notifications"].c["notification_type"] == bindparam("p1"),
                tables["notifications"].c["channel"] == sql_constant("local"),
            )
        )
        .order_by(
            tables["notifications"].c["created_at"].desc(),
            tables["notifications"].c["notification_id"].desc(),
        )
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": notification_type})


def find_for_persist_control_failure_alerts(conn, lane):
    query1 = (
        select(tables["notifications"].c["notification_id"])
        .select_from(tables["notifications"])
        .where(
            and_(
                tables["notifications"].c["notification_type"] == sql_constant("control_plane_failed"),
                tables["notifications"].c["status"] != sql_constant("resolved"),
                func.json_extract(
                    tables["notifications"].c["metadata_json"],
                    sql_constant("$.control_lane"),
                )
                == bindparam("p0"),
            )
        )
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": lane})


def find_for_collect(conn):
    query1 = (
        select(func.count(literal_column("*")))
        .select_from(tables["notifications"])
        .where(
            (
                func.json_extract(
                    tables["notifications"].c["metadata_json"],
                    sql_constant("$.delivery.status"),
                )
                == sql_constant("exhausted")
            )
        )
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_collect_2(conn, now):
    query1 = (
        select(
            func.max(
                sql_constant(0),
                (
                    (
                        func.julianday(bindparam("p0"))
                        - func.min(func.julianday(tables["notifications"].c["created_at"]))
                    )
                    * sql_constant(86400)
                ),
            )
        )
        .select_from(tables["notifications"])
        .where(
            and_(
                tables["notifications"].c["channel"] == sql_constant("local"),
                not_(tables["notifications"].c["status"].in_([sql_constant("sent"), sql_constant("resolved")])),
                not_(
                    func.coalesce(
                        func.json_extract(
                            tables["notifications"].c["metadata_json"],
                            sql_constant("$.delivery.status"),
                        ),
                        sql_constant(""),
                    ).in_([sql_constant("sent"), sql_constant("exhausted")])
                ),
            )
        )
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": now})


def find_for_build_dashboard_data(conn, history_limit):
    query1 = (
        select(
            tables["notifications"].c["notification_id"],
            tables["notifications"].c["task_id"],
            tables["notifications"].c["notification_type"],
            tables["notifications"].c["channel"],
            tables["notifications"].c["status"],
            tables["notifications"].c["created_at"],
            tables["notifications"].c["metadata_json"],
        )
        .select_from(tables["notifications"])
        .order_by(
            tables["notifications"].c["created_at"].desc(),
            tables["notifications"].c["notification_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": history_limit})


def find_for_latest_notification_for_task(conn, task_id):
    query1 = (
        select(
            tables["notifications"].c["notification_id"],
            tables["notifications"].c["notification_type"],
            tables["notifications"].c["channel"],
            tables["notifications"].c["status"],
            tables["notifications"].c["created_at"],
            tables["notifications"].c["metadata_json"],
        )
        .select_from(tables["notifications"])
        .where((tables["notifications"].c["task_id"] == bindparam("p0")))
        .order_by(
            tables["notifications"].c["created_at"].desc(),
            tables["notifications"].c["notification_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_notify_exhausted_episode(conn, notification_type, episode_id):
    query1 = (
        select(tables["notifications"].c["notification_id"])
        .select_from(tables["notifications"])
        .where(
            and_(
                (tables["notifications"].c["notification_type"] == bindparam("p0")),
                (
                    func.json_extract(
                        tables["notifications"].c["metadata_json"],
                        sql_constant("$.episode_id"),
                    )
                    == bindparam("p1")
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": notification_type, "p1": episode_id})


def find_for_deliver_pending(conn, now, limit, repo_ids=None, *, messaging_repo_ids=None, messaging_settings=None):
    sqlite.register_timestamp_after(conn)
    notification = tables["notifications"]
    stmt = (
        select(notification.c.notification_id)
        .where(
            _pending_delivery(now, repo_ids),
            _pending_attention(messaging_repo_ids),
            _dispatch_identity(messaging_settings),
            _delivery_window(messaging_settings),
        )
        .order_by(notification.c.created_at, notification.c.notification_id)
        .limit(limit)
    )
    return statements.read(conn, stmt)


def update_failure(conn, kind, key):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["notifications"])
        .where(
            and_(
                tables["notifications"].c["notification_type"] == bindparam("p0"),
                tables["notifications"].c["status"] != sql_constant("resolved"),
                func.coalesce(
                    func.json_extract(
                        tables["notifications"].c["metadata_json"],
                        sql_constant("$.failure_key"),
                    ),
                    func.json_extract(
                        tables["notifications"].c["metadata_json"],
                        sql_constant("$.source_key"),
                    ),
                    func.json_extract(
                        tables["notifications"].c["metadata_json"],
                        sql_constant("$.repo_id"),
                    ),
                )
                == bindparam("p1"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["notifications"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": kind, "p1": key})


def has_pending_local_delivery_filtered(conn, repo_ids, now, *, messaging_repo_ids=None, messaging_settings=None):
    sqlite.register_timestamp_after(conn)
    stmt = (
        select(sql_constant(1))
        .select_from(tables["notifications"])
        .where(
            _pending_delivery(now, repo_ids),
            _pending_attention(messaging_repo_ids),
            _dispatch_identity(messaging_settings),
            _delivery_window(messaging_settings),
        )
    )
    return statements.read(conn, stmt.limit(1))


def _delivery_window(settings):
    """External activation never consumes history; inbox visibility is unchanged."""
    settings = settings or {}
    if not settings.get("enabled") and "send_after" not in settings:
        return sql_constant(1) == sql_constant(1)
    # A missing or invalid boundary withholds delivery even for unvalidated callers.
    created = tables["notifications"].c.created_at
    return func.python_timestamp_after(created, settings.get("send_after")) == sql_constant(1)


def find_message_context(conn, notification_id):
    """Read current business facts without changing notification or task state."""
    notice, task, stream = tables["notifications"], tables["tasks"], tables["workstreams"]
    item, repo, source, attempt = (tables[name] for name in ("work_items", "repos", "github_sources", "attempts"))
    item_id = func.json_extract(notice.c.metadata_json, "$.work_item_id")
    attempt_id = func.json_extract(notice.c.metadata_json, "$.attempt_id")
    joins = (
        notice.outerjoin(task, task.c.task_id == notice.c.task_id)
        .outerjoin(
            item,
            or_(item.c.work_item_id == item_id, and_(item_id.is_(None), item.c.workstream_id == task.c.workstream_id)),
        )
        .outerjoin(
            stream,
            stream.c.workstream_id
            == func.coalesce(
                task.c.workstream_id, item.c.workstream_id, func.json_extract(notice.c.metadata_json, "$.workstream_id")
            ),
        )
        .outerjoin(
            repo,
            repo.c.repo_id
            == func.coalesce(item.c.repo_id, stream.c.repo_id, func.json_extract(notice.c.metadata_json, "$.repo_id")),
        )
        .outerjoin(source, source.c.source_id == func.coalesce(stream.c.primary_source_id, item.c.origin_source_id))
        .outerjoin(attempt, and_(attempt.c.attempt_id == attempt_id, attempt.c.task_id == task.c.task_id))
    )
    return statements.read(
        conn,
        select(
            notice.c.created_at.label("occurred_at"),
            repo.c.full_name.label("repository"),
            func.coalesce(item.c.title, source.c.title).label("title"),
            task.c.lifecycle.label("task_status"),
            task.c.route_id,
            stream.c.workstream_id,
            stream.c.lifecycle.label("workstream_status"),
            source.c.source_type,
            source.c.html_url,
            attempt.c.failure_json,
        )
        .select_from(joins)
        .where(notice.c.notification_id == notification_id),
    )


def _pending_delivery(now, repo_ids):
    notification = tables["notifications"]
    retry_at = func.json_extract(notification.c.metadata_json, sql_constant("$.delivery.next_retry_at"))
    predicate = and_(
        notification.c.channel == sql_constant("local"),
        notification.c.status.not_in((sql_constant("resolved"), sql_constant("sent"))),
        func.coalesce(
            func.json_extract(notification.c.metadata_json, sql_constant("$.delivery.status")),
            sql_constant(""),
        ).not_in((sql_constant("sent"), sql_constant("exhausted"))),
        or_(retry_at.is_(None), func.julianday(retry_at) <= func.julianday(now)),
    )
    if repo_ids is not None:
        task, stream = tables["tasks"], tables["workstreams"]
        scoped = (
            select(task.c.task_id)
            .join(stream, stream.c.workstream_id == task.c.workstream_id)
            .where(stream.c.repo_id.in_(repo_ids))
        )
        predicate = and_(
            predicate,
            or_(notification.c.task_id.is_(None), notification.c.task_id.in_(scoped)),
        )
    return predicate


def _pending_attention(repo_ids):
    """Exclude closed attention and denied repositories before any queue limit."""
    notice = tables["notifications"]
    event, item = tables["work_item_events"], tables["work_items"]
    resolved = event.alias("resolved_attention")
    attention_id = func.json_extract(notice.c.metadata_json, "$.attention_event_id")
    item_id = func.json_extract(notice.c.metadata_json, "$.work_item_id")
    metadata_repo = func.json_extract(notice.c.metadata_json, "$.repo_id")
    open_attention = (
        select(1)
        .select_from(event.join(item, item.c.work_item_id == event.c.work_item_id))
        .where(
            event.c.event_id == attention_id,
            item.c.work_item_id == item_id,
            item.c.canceled_at.is_(None),
            item.c.completed_at.is_(None),
            ~select(1).where(resolved.c.resolves_event_id == event.c.event_id).exists(),
        )
        .correlate(notice)
        .exists()
    )
    predicate = or_(attention_id.is_(None), open_attention)
    if repo_ids is None:
        return predicate
    task, stream = tables["tasks"], tables["workstreams"]
    item_repo = select(item.c.repo_id).where(item.c.work_item_id == item_id).correlate(notice).scalar_subquery()
    task_repo = (
        select(stream.c.repo_id)
        .select_from(task.join(stream, stream.c.workstream_id == task.c.workstream_id))
        .where(task.c.task_id == notice.c.task_id)
        .correlate(notice)
        .scalar_subquery()
    )
    incident_id = func.json_extract(notice.c.metadata_json, "$.incident_id")
    binding = _incident_binding(incident_id).correlate(notice)
    incident = tables["supervision_incidents"]
    global_incident = binding.where(incident.c.scope_type == "controller").exists()
    incident_repo = (
        binding.with_only_columns(binding.selected_columns.repo_id)
        .where(incident.c.scope_type.in_(("github_event", "work_item_event")))
        .scalar_subquery()
    )
    unbound = and_(
        notice.c.task_id.is_(None),
        item_id.is_(None),
        metadata_repo.is_(None),
        func.coalesce(func.json_extract(notice.c.metadata_json, "$.repository_scoped"), 0) != 1,
    )
    return and_(
        predicate,
        or_(
            and_(unbound, or_(incident_id.is_(None), global_incident)),
            func.coalesce(item_repo, task_repo, metadata_repo).in_(sorted(repo_ids)),
            and_(unbound, incident_repo.in_(sorted(repo_ids))),
        ),
    )


def find_inbox(conn, *, messaging_repo_ids, messaging_settings=None, limit=100):
    notice = tables["notifications"]
    stmt = (
        select(notice.c.notification_id)
        .where(
            notice.c.channel == "local",
            notice.c.status != "resolved",
            or_(
                func.json_extract(notice.c.metadata_json, "$.operator_response").is_(None),
                func.json_extract(notice.c.metadata_json, "$.attention_event_id").is_not(None),
            ),
            _pending_attention(messaging_repo_ids),
            _dispatch_identity(messaging_settings),
        )
        .order_by(notice.c.created_at.desc(), notice.c.notification_id.desc())
        .limit(limit)
    )
    return statements.read(conn, stmt)


def find_local_response(conn, notification_id):
    notice = tables["notifications"]
    return statements.read(
        conn,
        select(notice.c.task_id, notice.c.metadata_json).where(
            notice.c.notification_id == notification_id, notice.c.channel == "local"
        ),
    )


def find_chat_question(conn, *, messaging_repo_ids, messaging_settings, notification_id=None, message_id=None):
    """Match only delivered questions from the currently authorized endpoint."""
    notice = tables["notifications"]
    predicates = [
        notice.c.channel == "local",
        func.json_extract(notice.c.metadata_json, "$.delivery.status") == "sent",
        _dispatch_identity(messaging_settings),
    ]
    for field in ("provider", "channel", "account", "conversation", "thread"):
        predicates.append(
            func.json_extract(notice.c.metadata_json, "$.delivery.endpoint." + field)
            == messaging_settings.get(field, "")
        )
    event, item = tables["work_item_events"], tables["work_items"]
    attention_id = func.json_extract(notice.c.metadata_json, "$.attention_event_id")
    item_id = func.json_extract(notice.c.metadata_json, "$.work_item_id")
    predicates.append(
        select(1)
        .select_from(event.join(item, item.c.work_item_id == event.c.work_item_id))
        .where(
            event.c.event_id == attention_id,
            item.c.work_item_id == item_id,
            item.c.repo_id.in_(sorted(messaging_repo_ids)),
            event.c.event_type == "operator_question",
            func.coalesce(func.json_extract(event.c.metadata_json, "$.kind"), "") != "result_contract_cutover",
        )
        .correlate(notice)
        .exists()
    )
    if notification_id is not None:
        predicates.append(notice.c.notification_id == notification_id)
    if message_id is not None:
        predicates.append(func.json_extract(notice.c.metadata_json, "$.delivery.message_id") == message_id)
    if notification_id is None and message_id is None:
        predicates.extend([notice.c.status != "resolved", _pending_attention(messaging_repo_ids)])
    return statements.read(
        conn,
        select(notice.c.notification_id, notice.c.metadata_json, notice.c.status)
        .where(*predicates)
        .order_by(
            func.julianday(func.json_extract(notice.c.metadata_json, "$.delivery.sent_at")).desc(),
            func.julianday(notice.c.created_at).desc(),
            notice.c.notification_id.desc(),
        )
        .limit(2 if notification_id is not None or message_id is not None else 1),
    )


def find_scope(conn, *, work_item_id, task_id):
    if work_item_id:
        item = tables["work_items"]
        stmt = select(item.c.repo_id).where(item.c.work_item_id == work_item_id)
    else:
        task, stream = tables["tasks"], tables["workstreams"]
        stmt = (
            select(stream.c.repo_id)
            .select_from(task.join(stream, stream.c.workstream_id == task.c.workstream_id))
            .where(task.c.task_id == task_id)
        )
    return statements.read(conn, stmt)


def _incident_binding(incident_id):
    incident, checkpoint = tables["supervision_incidents"], tables["supervision_checkpoints"]
    return (
        select(incident.c.scope_type, func.json_extract(checkpoint.c.metadata_json, "$.repo_id").label("repo_id"))
        .select_from(
            incident.outerjoin(
                checkpoint,
                and_(
                    checkpoint.c.scope_type == incident.c.scope_type,
                    checkpoint.c.scope_id == incident.c.scope_id,
                    checkpoint.c.rule_id == "admission",
                ),
            )
        )
        .where(incident.c.incident_id == incident_id)
    )


def find_incident_scope(conn, incident_id):
    """Recover older task-null notices' scope without rewriting their receipts."""
    return statements.read(conn, _incident_binding(incident_id))


def _dispatch_identity(settings):
    """Use the same endpoint/sender binding for inbox, delivery and readiness."""
    if settings is None:
        return True
    notice, task, request = (tables[name] for name in ("notifications", "tasks", "dispatch_requests"))
    task_identity = (
        select(request.c.identity_json)
        .select_from(request.join(task, task_binding(request, task)))
        .where(task.c.task_id == notice.c.task_id)
        .correlate(notice)
        .scalar_subquery()
    )
    identity = func.coalesce(func.json_extract(notice.c.metadata_json, "$.dispatch_identity"), task_identity)
    if not settings.get("enabled"):
        return identity.is_(None)
    matches = [
        func.json_extract(identity, "$." + field) == settings.get(field, "")
        for field in ("provider", "channel", "account", "conversation", "thread")
    ]
    return or_(
        identity.is_(None),
        and_(
            *matches,
            func.json_extract(identity, "$.protocol") == "robert.message.v1",
            func.json_extract(identity, "$.sender_id").in_(settings.get("allowed_senders", [])),
        ),
    )

"""Shared queries operations for notifications."""

from robert_agent.persistence.expressions import sql_constant

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


def find_for_deliver_pending(conn, now, limit, repo_ids=()):
    notification = tables["notifications"]
    stmt = (
        select(notification.c.notification_id)
        .where(_pending_delivery(now, repo_ids))
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


def has_pending_local_delivery_filtered(conn, repo_ids, now):
    stmt = select(sql_constant(1)).select_from(tables["notifications"]).where(_pending_delivery(now, repo_ids))
    if not repo_ids:
        stmt = stmt.where(tables["notifications"].c.task_id.is_(None))
    return statements.read(conn, stmt.limit(1))


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
    if repo_ids:
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

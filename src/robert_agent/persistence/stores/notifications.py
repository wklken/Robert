"""Shared stores operations for notifications."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_notification(conn, value_1, value_2, notification_type, channel, status, value_6, value_7):
    stmt = insert(tables["notifications"]).values(
        {
            "notification_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "notification_type": bindparam("p2"),
            "channel": bindparam("p3"),
            "status": bindparam("p4"),
            "created_at": bindparam("p5"),
            "metadata_json": bindparam("p6"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": notification_type,
            "p3": channel,
            "p4": status,
            "p5": value_6,
            "p6": value_7,
        },
    )


def set_metadata_json_by_notification_id(conn, value_1, notification_id):
    stmt = (
        update(tables["notifications"])
        .values({"metadata_json": bindparam("p0")})
        .where((tables["notifications"].c["notification_id"] == bindparam("p1")))
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": notification_id})


def insert_workflow_attention_notification(conn, notification_id, value_2, now, value_4):
    stmt = insert(tables["notifications"]).values(
        {
            "notification_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "notification_type": sql_constant("workflow_attention_required"),
            "channel": sql_constant("local"),
            "status": sql_constant("recorded"),
            "created_at": bindparam("p2"),
            "metadata_json": bindparam("p3"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["notification_id"],
        set_={
            "status": sql_constant("recorded"),
            "created_at": stmt.excluded["created_at"],
            "metadata_json": stmt.excluded["metadata_json"],
        },
    )
    return statements.write(conn, stmt, {"p0": notification_id, "p1": value_2, "p2": now, "p3": value_4})


def insert_notification(conn, value_1, task_id, notification_type, now, value_5):
    stmt = insert(tables["notifications"]).values(
        {
            "notification_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "notification_type": bindparam("p2"),
            "channel": sql_constant("local"),
            "status": sql_constant("recorded"),
            "created_at": bindparam("p3"),
            "metadata_json": bindparam("p4"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": task_id,
            "p2": notification_type,
            "p3": now,
            "p4": value_5,
        },
    )


def update_for_resolve_action_notification(conn, now, value_2, notification_id):
    stmt = (
        update(tables["notifications"])
        .values(
            {
                "status": sql_constant("resolved"),
                "created_at": bindparam("p0"),
                "metadata_json": bindparam("p1"),
            }
        )
        .where((tables["notifications"].c["notification_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": value_2, "p2": notification_id})


def insert_notification_2(conn, now, value_2, notification_id):
    stmt = (
        update(tables["notifications"])
        .values(
            {
                "status": sql_constant("recorded"),
                "created_at": bindparam("p0"),
                "metadata_json": bindparam("p1"),
            }
        )
        .where((tables["notifications"].c["notification_id"] == bindparam("p2")))
    )
    return statements.write(conn, stmt, {"p0": now, "p1": value_2, "p2": notification_id})


def insert_for_prepare(conn, value_1, task_id, now, value_4):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "task_id": bindparam("p1"),
                "notification_type": sql_constant("execution_attention_required"),
                "channel": sql_constant("local"),
                "status": sql_constant("recorded"),
                "created_at": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": task_id, "p2": now, "p3": value_4})


def insert_for_insert(conn, value_1, value_2, notification_type, status, run_now, value_6):
    stmt = insert(tables["notifications"]).values(
        {
            "notification_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "notification_type": bindparam("p2"),
            "channel": sql_constant("local"),
            "status": bindparam("p3"),
            "created_at": bindparam("p4"),
            "metadata_json": bindparam("p5"),
        }
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": notification_type,
            "p3": status,
            "p4": run_now,
            "p5": value_6,
        },
    )


def update_for_resolve_control_failure(conn, lane):
    stmt = (
        update(tables["notifications"])
        .values({"status": sql_constant("resolved")})
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
    )
    return statements.write(conn, stmt, {"p0": lane})


def record_control_stop(conn, value_1, now, value_3):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "notification_type": sql_constant("control_plane_failed"),
                "channel": sql_constant("local"),
                "status": sql_constant("recorded"),
                "created_at": bindparam("p1"),
                "metadata_json": bindparam("p2"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": now, "p2": value_3})


def insert_for_terminate_worker(conn, value_1, task_id, now, value_4):
    stmt = insert(tables["notifications"]).values(
        {
            "notification_id": bindparam("p0"),
            "task_id": bindparam("p1"),
            "notification_type": sql_constant("worker_termination"),
            "channel": sql_constant("local"),
            "status": sql_constant("recorded"),
            "created_at": bindparam("p2"),
            "metadata_json": bindparam("p3"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["notification_id"],
        set_={
            "status": stmt.excluded["status"],
            "created_at": stmt.excluded["created_at"],
            "metadata_json": stmt.excluded["metadata_json"],
        },
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": task_id, "p2": now, "p3": value_4})


def insert_for_notify_rejection(conn, value_1, value_2):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "notification_type": sql_constant("worker_result_invalid"),
                "channel": sql_constant("local"),
                "status": sql_constant("recorded"),
                "created_at": func.strftime(sql_constant("%Y-%m-%dT%H:%M:%f+00:00"), sql_constant("now")),
                "metadata_json": bindparam("p1"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2})


def update_for_persist_control_failure_alerts(conn, identifier):
    stmt = (
        update(tables["notifications"])
        .values({"status": sql_constant("resolved")})
        .where((tables["notifications"].c["notification_id"] == bindparam("p0")))
    )
    return statements.write(conn, stmt, {"p0": identifier})


def insert_for_collect(conn, value_1, task_id, now, value_4):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "task_id": bindparam("p1"),
                "notification_type": sql_constant("execution_delayed"),
                "channel": sql_constant("local"),
                "status": sql_constant("recorded"),
                "created_at": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": task_id, "p2": now, "p3": value_4})


def insert_for_notice(conn, identifier, task_id, now, value_4):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "task_id": bindparam("p1"),
                "notification_type": sql_constant("supervision_recovery_attention"),
                "channel": sql_constant("local"),
                "status": sql_constant("recorded"),
                "created_at": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": identifier, "p1": task_id, "p2": now, "p3": value_4})


def insert_for_notify_changes(conn, identifier, value_2, now, value_4):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "task_id": bindparam("p1"),
                "notification_type": sql_constant("supervision_incident"),
                "channel": sql_constant("local"),
                "status": sql_constant("recorded"),
                "created_at": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": identifier, "p1": value_2, "p2": now, "p3": value_4})


def insert_for_deliver_health(conn, notification_id, value_2, created_at, value_4):
    stmt = (
        insert(tables["notifications"])
        .values(
            {
                "notification_id": bindparam("p0"),
                "notification_type": sql_constant("supervisor_unobservable"),
                "channel": sql_constant("local"),
                "status": bindparam("p1"),
                "created_at": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {"p0": notification_id, "p1": value_2, "p2": created_at, "p3": value_4},
    )


def insert_for_fail_batch(conn, value_1, value_2, value_3):
    stmt = insert(tables["notifications"]).values(
        {
            "notification_id": bindparam("p0"),
            "notification_type": sql_constant("github_scan_attention"),
            "channel": sql_constant("local"),
            "status": sql_constant("recorded"),
            "created_at": bindparam("p1"),
            "metadata_json": bindparam("p2"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["notification_id"],
        set_={
            "status": stmt.excluded["status"],
            "created_at": stmt.excluded["created_at"],
            "metadata_json": stmt.excluded["metadata_json"],
        },
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": value_2, "p2": value_3})


def update_failure(conn, kind, key):
    stmt = (
        update(tables["notifications"])
        .values({"status": sql_constant("resolved")})
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
    )
    return statements.write(conn, stmt, {"p0": kind, "p1": key})


def resolve_attention(conn, event_id):
    notice = tables["notifications"]
    stmt = (
        update(notice)
        .values(status="resolved")
        .where(
            (notice.c.notification_id == "attention-" + event_id)
            | (func.json_extract(notice.c.metadata_json, "$.attention_event_id") == event_id)
        )
    )
    return statements.write(conn, stmt)


def record_attention(conn, *, notification_id, task_id, event_type, now, metadata_json):
    """Attach attention to an existing notice without losing its delivery receipt."""
    notice = tables["notifications"]
    stmt = insert(notice).values(
        notification_id=notification_id,
        task_id=task_id,
        notification_type=event_type,
        channel="local",
        status="recorded",
        created_at=now,
        metadata_json=metadata_json,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["notification_id"],
        set_={"metadata_json": func.json_patch(notice.c.metadata_json, stmt.excluded.metadata_json)},
    )
    return statements.write(conn, stmt)


def set_delivery_state(conn, notification_id, delivery_json):
    """Update only sender-owned metadata so concurrent operator receipts survive."""
    notice = tables["notifications"]
    stmt = (
        update(notice)
        .where(notice.c.notification_id == notification_id)
        .values(metadata_json=func.json_set(notice.c.metadata_json, "$.delivery", func.json(delivery_json)))
    )
    return statements.write(conn, stmt)

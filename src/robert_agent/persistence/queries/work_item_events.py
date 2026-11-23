"""Shared queries operations for work_item_events."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, not_, null, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_chat_reply_receipt(conn, key, actor_identity, repo_ids):
    event, item, notice = (tables[name] for name in ("work_item_events", "work_items", "notifications"))
    return statements.read(
        conn,
        select(event.c.body, event.c.metadata_json, notice.c.notification_id)
        .select_from(
            event.join(item, item.c.work_item_id == event.c.work_item_id).join(
                notice, func.json_extract(notice.c.metadata_json, "$.attention_event_id") == event.c.resolves_event_id
            )
        )
        .where(
            event.c.idempotency_key == key,
            event.c.actor_identity == actor_identity,
            event.c.event_type == "user_response",
            item.c.repo_id.in_(sorted(repo_ids)),
        ),
    )


def find_for_receipt_facts(conn, key):
    table1 = tables["work_item_events"].alias("e")
    table2 = tables["work_items"].alias("wi")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["work_item_id"] == table1.c["work_item_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                (table1.c["event_id"] == bindparam("p0")),
                not_(table2.c["canceled_at"].is_(null())),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": key})


def record_system_event(conn, work_item_id, idempotency_key):
    query1 = (
        select(tables["work_item_events"])
        .select_from(tables["work_item_events"])
        .where(
            and_(
                (tables["work_item_events"].c["work_item_id"] == bindparam("p0")),
                (tables["work_item_events"].c["idempotency_key"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["work_item_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id, "p1": idempotency_key})


def find_for_stored_command_result(conn, work_item_id, idempotency_key):
    query1 = (
        select(tables["work_item_events"].c["metadata_json"])
        .select_from(tables["work_item_events"])
        .where(
            and_(
                (tables["work_item_events"].c["work_item_id"] == bindparam("p0")),
                (tables["work_item_events"].c["idempotency_key"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["work_item_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id, "p1": idempotency_key})


def find_for_retrigger_failed_github_task(conn, work_item_id, value_2):
    query1 = (
        select(tables["work_item_events"].c["event_id"])
        .select_from(tables["work_item_events"])
        .where(
            and_(
                (tables["work_item_events"].c["work_item_id"] == bindparam("p0")),
                (tables["work_item_events"].c["idempotency_key"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["work_item_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id, "p1": value_2})


def find_for_finalize_accepted_result(conn, value_1):
    table1 = tables["work_item_events"].alias("e")
    table3 = tables["work_item_events"].alias("response")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where((table3.c["resolves_event_id"] == table1.c["event_id"]))
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1.c["event_id"])
        .select_from(table1)
        .where(
            and_(
                table1.c["work_item_id"] == bindparam("p0"),
                table1.c["event_type"] == sql_constant("operator_decision_required"),
                not_(query4.exists()),
            )
        )
        .order_by(table1.c["created_at"].desc(), table1.c["event_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": value_1})


def find_for_prepare_detected_local_tasks(conn, work_item_id):
    query1 = (
        select(
            tables["work_item_events"].c["event_id"],
            tables["work_item_events"].c["event_type"],
            tables["work_item_events"].c["actor_kind"],
            tables["work_item_events"].c["actor_identity"],
            tables["work_item_events"].c["body"],
            tables["work_item_events"].c["resolves_event_id"],
            tables["work_item_events"].c["created_at"],
            tables["work_item_events"].c["metadata_json"],
        )
        .select_from(tables["work_item_events"])
        .where((tables["work_item_events"].c["work_item_id"] == bindparam("p0")))
        .order_by(
            tables["work_item_events"].c["created_at"].asc(),
            tables["work_item_events"].c["event_id"].asc(),
        )
        .correlate_except(tables["work_item_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id})


def find_for_prepare_github_operator_tasks(conn, work_item_id):
    query1 = (
        select(
            tables["work_item_events"].c["event_id"],
            tables["work_item_events"].c["event_type"],
            tables["work_item_events"].c["actor_identity"],
            tables["work_item_events"].c["body"],
            tables["work_item_events"].c["created_at"],
        )
        .select_from(tables["work_item_events"])
        .where(
            and_(
                (tables["work_item_events"].c["work_item_id"] == bindparam("p0")),
                tables["work_item_events"]
                .c["event_type"]
                .in_(
                    [
                        sql_constant("user_response"),
                        sql_constant("changes_requested"),
                        sql_constant("reopened"),
                        sql_constant("retry_requested"),
                    ]
                ),
            )
        )
        .order_by(
            tables["work_item_events"].c["created_at"].asc(),
            tables["work_item_events"].c["event_id"].asc(),
        )
        .correlate_except(tables["work_item_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id})


def find_for_operator_canceled_item(conn, work_item_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["work_item_events"])
        .where(
            and_(
                tables["work_item_events"].c["work_item_id"] == bindparam("p0"),
                tables["work_item_events"].c["event_type"] == sql_constant("canceled"),
                tables["work_item_events"].c["actor_kind"] != sql_constant("system"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["work_item_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": work_item_id})


def unresolved_event_filtered(conn, items, value_1):
    table1 = tables["work_item_events"].alias("e")
    table3 = tables["work_item_events"].alias("response")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where((table3.c["resolves_event_id"] == table1.c["event_id"]))
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1)
        .select_from(table1)
        .where(
            and_(
                table1.c["work_item_id"] == bindparam("p0"),
                table1.c["event_type"].in_(bindparam("items", expanding=True)),
                not_(query4.exists()),
            )
        )
        .order_by(table1.c["created_at"].desc(), table1.c["event_id"].desc())
        .limit(sql_constant(1))
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": value_1, "items": items})


def find_attention(conn, event_id, work_item_id):
    event, item = tables["work_item_events"], tables["work_items"]
    resolved = event.alias("resolved_attention")
    stmt = (
        select(
            event.c.event_id,
            event.c.work_item_id,
            event.c.event_type,
            event.c.body,
            event.c.metadata_json,
            item.c.version,
            item.c.title,
            item.c.completed_at,
            item.c.canceled_at,
            select(1).where(resolved.c.resolves_event_id == event.c.event_id).exists().label("resolved"),
        )
        .select_from(event.join(item, item.c.work_item_id == event.c.work_item_id))
        .where(event.c.event_id == event_id, event.c.work_item_id == work_item_id)
    )
    return statements.read(conn, stmt)

"""Shared queries operations for task_events."""

from robert_agent.foundation.contracts.work import TaskLifecycle

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    not_,
    case,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries.request_events import task_relationship
from robert_agent.persistence import statements


def find_for_for_result(conn, task_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    query3 = (
        select(table2.c["payload_json"])
        .select_from(table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False))
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(table2.c["event_at"].asc(), table2.c["event_id"].asc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_for_build_task(conn, task_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    table3 = tables["sources"].alias("gs")
    query4 = (
        select(
            task_relationship(table1).label("relationship"),
            table2.c["event_fingerprint"],
            table2.c["event_type"],
            table2.c["actor_login"],
            table2.c["authorization_status"],
            table2.c["event_at"],
            table3.c["source_key"],
            table3.c["source_type"],
            table3.c["number"],
            table3.c["html_url"],
        )
        .select_from(
            table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False).join(
                table3, (table3.c["source_id"] == table2.c["source_id"]), isouter=False
            )
        )
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(table1.c["created_at"].desc())
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": task_id})


def find_for_build_event(conn, event_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["tasks"].alias("t")
    table3 = tables["route_decisions"].alias("rd")
    query4 = (
        select(tables["route_decisions"].c["route_decision_id"])
        .select_from(tables["route_decisions"])
        .where((tables["route_decisions"].c["task_id"] == table2.c["task_id"]))
        .order_by(
            tables["route_decisions"].c["created_at"].desc(),
            tables["route_decisions"].c["route_decision_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["route_decisions"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    table5 = tables["attempts"].alias("a")
    query6 = (
        select(tables["attempts"].c["attempt_id"])
        .select_from(tables["attempts"])
        .where((tables["attempts"].c["task_id"] == table2.c["task_id"]))
        .order_by(
            tables["attempts"].c["attempt_no"].desc(),
            tables["attempts"].c["started_at"].desc(),
            tables["attempts"].c["attempt_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["attempts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query7 = (
        select(
            task_relationship(table1).label("relationship"),
            table2.c["task_id"],
            table2.c["lifecycle"],
            table2.c["priority"],
            table2.c["workstream_id"],
            func.coalesce(table2.c["route_id"], table3.c["route_id"]).label("route_id"),
            func.coalesce(table2.c["expected_output"], table3.c["expected_output"]).label("expected_output"),
            table2.c["parent_task_id"],
            table5.c["attempt_id"].label("latest_attempt_id"),
            table5.c["status"].label("latest_attempt_status"),
            table5.c["heartbeat_at"].label("latest_attempt_heartbeat_at"),
            table5.c["worktree_path"],
            table5.c["branch_name"],
        )
        .select_from(
            table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False)
            .join(
                table3,
                (table3.c["route_decision_id"] == query4.scalar_subquery()),
                isouter=True,
            )
            .join(
                table5,
                (table5.c["attempt_id"] == query6.scalar_subquery()),
                isouter=True,
            )
        )
        .where((table1.c["event_id"] == bindparam("p0")))
        .order_by(table1.c["created_at"].desc(), table2.c["task_id"].desc())
        .correlate_except(table1, table2, table3, table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query7, {"p0": event_id})


def find_for_target(conn, task_id):
    query1 = (
        select(tables["task_events"].c["event_id"])
        .select_from(tables["task_events"])
        .where(
            and_(
                (tables["task_events"].c["task_id"] == bindparam("p0")),
                task_relationship(tables["task_events"]).in_([sql_constant("context"), sql_constant("pending")]),
            )
        )
        .order_by(tables["task_events"].c["event_id"].asc())
        .limit(sql_constant(101))
        .correlate_except(tables["task_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_snapshot(conn, task_id, repo_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    query3 = (
        select(
            table2.c["event_type"],
            table2.c["event_at"],
            table2.c["authorization_status"],
            case(
                (
                    (
                        func.length(func.json_extract(table2.c["payload_json"], sql_constant("$.title")))
                        <= sql_constant(4000)
                    ),
                    func.json_extract(table2.c["payload_json"], sql_constant("$.title")),
                )
            ).label("title"),
            case(
                (
                    (
                        func.length(func.json_extract(table2.c["payload_json"], sql_constant("$.body")))
                        <= sql_constant(8000)
                    ),
                    func.json_extract(table2.c["payload_json"], sql_constant("$.body")),
                )
            ).label("body"),
        )
        .select_from(table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False))
        .where(
            and_(
                table1.c["task_id"] == bindparam("p0"),
                table2.c["repo_id"] == bindparam("p1"),
                task_relationship(table1).in_(
                    [
                        sql_constant("trigger"),
                        sql_constant("pending"),
                        sql_constant("consumed"),
                    ]
                ),
            )
        )
        .order_by(
            (task_relationship(table1) == sql_constant("trigger")).desc(),
            table2.c["event_at"].desc(),
            table2.c["event_id"].desc(),
        )
        .limit(sql_constant(3))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id, "p1": repo_id})


def find_for_pending_events_for_task(conn, task_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    query3 = (
        select(table2.c["payload_json"], table2.c["event_fingerprint"])
        .select_from(table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False))
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                task_relationship(table1).in_([sql_constant("pending"), sql_constant("context")]),
            )
        )
        .order_by(table2.c["event_at"].asc(), table2.c["event_id"].asc())
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_for_events_for_task(conn, task_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    query3 = (
        select(task_relationship(table1), table2.c["payload_json"])
        .select_from(table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False))
        .where((table1.c["task_id"] == bindparam("p0")))
        .order_by(
            case(
                (sql_constant("trigger"), sql_constant(0)),
                (sql_constant("context"), sql_constant(1)),
                (sql_constant("pending"), sql_constant(2)),
                value=task_relationship(table1),
                else_=sql_constant(3),
            ).asc(),
            table2.c["event_at"].asc(),
            table2.c["event_id"].asc(),
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": task_id})


def find_for_prepare_queued_reviews(conn, value_1):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["task_events"])
        .where((tables["task_events"].c["event_id"] == bindparam("p0")))
        .limit(sql_constant(1))
        .correlate_except(tables["task_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1})


def find_for_event_has_terminal_trigger(conn, event_fingerprint):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    table3 = tables["tasks"].alias("t")
    query4 = (
        select(sql_constant(1))
        .select_from(
            table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False).join(
                table3, (table3.c["task_id"] == table1.c["task_id"]), isouter=False
            )
        )
        .where(
            and_(
                (table2.c["event_fingerprint"] == bindparam("p0")),
                (
                    or_(
                        (task_relationship(table1) == sql_constant("consumed")),
                        (
                            and_(
                                (task_relationship(table1) == sql_constant("trigger")),
                                table3.c["lifecycle"].in_(
                                    [
                                        sql_constant(TaskLifecycle.COMPLETED.value),
                                        sql_constant(TaskLifecycle.FAILED.value),
                                        sql_constant(TaskLifecycle.CANCELED.value),
                                        sql_constant(TaskLifecycle.IGNORED.value),
                                    ]
                                ),
                            )
                        ),
                    )
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2, table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query4, {"p0": event_fingerprint})


def find_for_failed_parent_context_events(conn, parent_task_id):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    query3 = (
        select(
            task_relationship(table1).label("relationship"),
            table2.c["event_fingerprint"],
            table2.c["payload_json"],
        )
        .select_from(table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False))
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                task_relationship(table1).in_(
                    [
                        sql_constant("trigger"),
                        sql_constant("context"),
                        sql_constant("pending"),
                    ]
                ),
            )
        )
        .order_by(
            case(
                (sql_constant("trigger"), sql_constant(0)),
                (sql_constant("context"), sql_constant(1)),
                value=task_relationship(table1),
                else_=sql_constant(2),
            ).asc(),
            table2.c["event_at"].asc(),
            table2.c["event_id"].asc(),
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": parent_task_id})


def find_for_receipt_facts(conn, key, target_id, target_id_3):
    table1 = tables["task_events"].alias("te")
    table2 = tables["tasks"].alias("t")
    ledger = tables["request_events"]
    worker_results = tables["worker_results"]
    # The ledger is the receipt authority. A Worker result's original creation
    # time remains the occurrence boundary when immutable evidence is retained.
    consumed_at = (
        select(func.coalesce(worker_results.c.created_at, ledger.c.consumed_at))
        .select_from(ledger.outerjoin(worker_results, worker_results.c.result_id == ledger.c.result_id))
        .where(ledger.c.task_id == table1.c.task_id, ledger.c.event_id == table1.c.event_id)
        .correlate(table1)
        .scalar_subquery()
    )
    query8 = (
        select(
            table1.c["task_id"],
            case(
                (
                    (task_relationship(table1) == sql_constant("trigger")),
                    table1.c["created_at"],
                ),
                else_=consumed_at,
            ).label("occurred_at"),
        )
        .select_from(table1.join(table2, (table2.c["task_id"] == table1.c["task_id"]), isouter=False))
        .where(
            and_(
                (table1.c["event_id"] == bindparam("p0")),
                (
                    or_(
                        (
                            and_(
                                (table1.c["task_id"] == bindparam("p1")),
                                (task_relationship(table1) == sql_constant("consumed")),
                            )
                        ),
                        (
                            and_(
                                table2.c["parent_task_id"] == bindparam("p2"),
                                task_relationship(table1) == sql_constant("trigger"),
                                not_(
                                    table2.c["lifecycle"].in_(
                                        [
                                            sql_constant(TaskLifecycle.CANCELED.value),
                                            sql_constant(TaskLifecycle.IGNORED.value),
                                        ]
                                    )
                                ),
                            )
                        ),
                    )
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query8, {"p0": key, "p1": target_id, "p2": target_id_3})


def task_related_events_filtered(conn, items, value_1):
    table1 = tables["task_events"].alias("te")
    table2 = tables["source_events"].alias("ge")
    query3 = (
        select(
            task_relationship(table1).label("relationship"),
            table2.c["event_fingerprint"],
            table2.c["payload_json"],
        )
        .select_from(table1.join(table2, (table2.c["event_id"] == table1.c["event_id"]), isouter=False))
        .where(
            and_(
                (table1.c["task_id"] == bindparam("p0")),
                task_relationship(table1).in_(bindparam("items", expanding=True)),
            )
        )
        .order_by(
            case(
                (sql_constant("trigger"), sql_constant(0)),
                (sql_constant("consumed"), sql_constant(1)),
                (sql_constant("context"), sql_constant(2)),
                value=task_relationship(table1),
                else_=sql_constant(3),
            ).asc(),
            table2.c["event_at"].asc(),
            table2.c["event_id"].asc(),
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": value_1, "items": items})

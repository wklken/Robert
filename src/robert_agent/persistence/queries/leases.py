"""Shared queries operations for leases."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    not_,
    null,
    case,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_owner_lease_active(conn, owner):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["leases"])
        .where(
            and_(
                tables["leases"].c["owner_id"] == bindparam("p0"),
                tables["leases"].c["resource_type"] == sql_constant("daemon"),
                tables["leases"].c["status"] == sql_constant("active"),
                or_(
                    func.julianday(tables["leases"].c["expires_at"]).is_(null()),
                    func.julianday(tables["leases"].c["expires_at"]) > func.julianday(sql_constant("now")),
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["leases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": owner})


def find_for_acquire(conn, resource_key):
    query1 = (
        select(
            tables["leases"].c["lease_id"],
            tables["leases"].c["owner_id"],
            tables["leases"].c["expires_at"],
        )
        .select_from(tables["leases"])
        .where(
            and_(
                tables["leases"].c["resource_type"] == sql_constant("agent_run"),
                tables["leases"].c["resource_key"] == bindparam("p0"),
                tables["leases"].c["status"] == sql_constant("active"),
            )
        )
        .correlate_except(tables["leases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": resource_key})


def find_for_latest_daemon_summary(conn):
    query1 = (
        select(
            tables["leases"].c["lease_id"],
            tables["leases"].c["resource_key"],
            tables["leases"].c["owner_id"],
            tables["leases"].c["status"],
            tables["leases"].c["acquired_at"],
            tables["leases"].c["expires_at"],
            tables["leases"].c["heartbeat_at"],
            tables["leases"].c["released_at"],
            tables["leases"].c["metadata_json"],
        )
        .select_from(tables["leases"])
        .where((tables["leases"].c["resource_type"] == sql_constant("daemon")))
        .order_by(
            case(
                (
                    (tables["leases"].c["status"] == sql_constant("active")),
                    sql_constant(0),
                ),
                else_=sql_constant(1),
            ).asc(),
            tables["leases"].c["acquired_at"].desc(),
            tables["leases"].c["lease_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["leases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_acquire_daemon_lease(conn, resource_key):
    query1 = (
        select(
            tables["leases"].c["lease_id"],
            tables["leases"].c["owner_id"],
            tables["leases"].c["expires_at"],
        )
        .select_from(tables["leases"])
        .where(
            and_(
                tables["leases"].c["resource_type"] == sql_constant("daemon"),
                tables["leases"].c["resource_key"] == bindparam("p0"),
                tables["leases"].c["status"] == sql_constant("active"),
            )
        )
        .correlate_except(tables["leases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": resource_key})


def find_for_select_batch(conn, success_cutoff, failure_cutoff, batch_size):
    table1 = tables["leases"].alias("l")
    table3 = tables["agent_runs"].alias("r")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where(
            and_(
                (table3.c["run_id"] == table1.c["owner_id"]),
                (table3.c["status"] == sql_constant("running")),
            )
        )
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table5 = tables["daemon_runs"].alias("d")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where(
            and_(
                (table5.c["daemon_run_id"] == table1.c["owner_id"]),
                (table5.c["status"] == sql_constant("running")),
            )
        )
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table7 = tables["github_control_children"].alias("c")
    query8 = (
        select(sql_constant(1))
        .select_from(table7)
        .where(
            and_(
                (table7.c["owner_id"] == table1.c["owner_id"]),
                table7.c["status"].in_([sql_constant("starting"), sql_constant("running")]),
            )
        )
        .correlate_except(table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1.c["lease_id"])
        .select_from(table1)
        .where(
            and_(
                table1.c["resource_type"].in_([sql_constant("agent_run"), sql_constant("daemon")]),
                or_(
                    and_(
                        table1.c["status"] == sql_constant("released"),
                        func.julianday(table1.c["released_at"]) < func.julianday(bindparam("p0")),
                    ),
                    and_(
                        table1.c["status"] == sql_constant("expired"),
                        func.julianday(table1.c["expires_at"]) < func.julianday(bindparam("p1")),
                    ),
                ),
                not_(query4.exists()),
                not_(query6.exists()),
                not_(query8.exists()),
            )
        )
        .order_by(table1.c["acquired_at"].asc(), table1.c["lease_id"].asc())
        .limit(bindparam("p2"))
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": success_cutoff, "p1": failure_cutoff, "p2": batch_size})


def find_for_controller_facts(conn, value_1):
    table1 = tables["leases"].alias("l")
    table2 = tables["daemon_runs"].alias("d")
    query3 = (
        select(
            table1.c["lease_id"],
            table1.c["owner_id"],
            table1.c["status"].label("lease_status"),
            table1.c["acquired_at"],
            table1.c["expires_at"],
            table1.c["heartbeat_at"],
            table2.c["status"].label("run_status"),
        )
        .select_from(
            table1.join(
                table2,
                (table2.c["daemon_run_id"] == table1.c["owner_id"]),
                isouter=True,
            )
        )
        .where(
            and_(
                (table1.c["resource_type"] == sql_constant("daemon")),
                (table1.c["resource_key"] == bindparam("p0")),
            )
        )
        .order_by(
            (table1.c["status"] == sql_constant("active")).desc(),
            table1.c["acquired_at"].desc(),
            table1.c["lease_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": value_1})


def find_for_is_current(conn, value_1):
    table1 = tables["leases"].alias("l")
    table2 = tables["daemon_runs"].alias("d")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["daemon_run_id"] == table1.c["owner_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["resource_type"] == sql_constant("daemon"),
                table1.c["status"] == sql_constant("active"),
                table2.c["status"] == sql_constant("running"),
                table1.c["owner_id"] == bindparam("p0"),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": value_1})


def find_for_require_idle_repository(conn, repo_id, now):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["leases"])
        .where(
            and_(
                tables["leases"].c["resource_type"] == sql_constant("agent_run"),
                tables["leases"].c["resource_key"] == bindparam("p0"),
                tables["leases"].c["status"] == sql_constant("active"),
                tables["leases"].c["expires_at"] > bindparam("p1"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["leases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": now})

"""Shared queries operations for daemon_runs."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_latest_daemon_summary(conn):
    query1 = (
        select(
            tables["daemon_runs"].c["daemon_run_id"],
            tables["daemon_runs"].c["status"],
            tables["daemon_runs"].c["owner_id"],
            tables["daemon_runs"].c["config_path"],
            tables["daemon_runs"].c["started_at"],
            tables["daemon_runs"].c["finished_at"],
            tables["daemon_runs"].c["summary_json"],
            tables["daemon_runs"].c["error_json"],
        )
        .select_from(tables["daemon_runs"])
        .order_by(
            tables["daemon_runs"].c["started_at"].desc(),
            tables["daemon_runs"].c["daemon_run_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["daemon_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_started_at_by_daemon_run_id(conn, daemon_run_id):
    query1 = (
        select(tables["daemon_runs"].c["started_at"])
        .select_from(tables["daemon_runs"])
        .where((tables["daemon_runs"].c["daemon_run_id"] == bindparam("p0")))
        .correlate_except(tables["daemon_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": daemon_run_id})


def find_for_run_exists(conn, daemon_run_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["daemon_runs"])
        .where((tables["daemon_runs"].c["daemon_run_id"] == bindparam("p0")))
        .correlate_except(tables["daemon_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": daemon_run_id})


def find_for_deployment(conn, value_1, value_2, value_3):
    table1 = tables["daemon_runs"].alias("d")
    table2 = tables["leases"].alias("l")
    query3 = (
        select(sql_constant(1))
        .select_from(
            table1.join(
                table2,
                (table2.c["owner_id"] == table1.c["daemon_run_id"]),
                isouter=False,
            )
        )
        .where(
            and_(
                table1.c["daemon_run_id"] == bindparam("p0"),
                table1.c["status"] == sql_constant("running"),
                table1.c["started_at"] == bindparam("p1"),
                table2.c["resource_type"] == sql_constant("daemon"),
                table2.c["status"] == sql_constant("active"),
                func.julianday(table2.c["expires_at"]) > func.julianday(bindparam("p2")),
            )
        )
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": value_1, "p1": value_2, "p2": value_3})

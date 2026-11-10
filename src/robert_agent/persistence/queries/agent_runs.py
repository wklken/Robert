"""Shared queries operations for agent_runs."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    not_,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements
from robert_agent.persistence.queries.run_summaries import resolved_summary


def find_for_finish_stopped_executor(conn, executor_id):
    query1 = (
        select(tables["agent_runs"].c["run_id"])
        .select_from(tables["agent_runs"])
        .where(
            and_(
                (tables["agent_runs"].c["status"] == sql_constant("running")),
                (
                    func.json_extract(
                        tables["agent_runs"].c["summary_json"],
                        sql_constant("$.executor_id"),
                    )
                    == bindparam("p0")
                ),
            )
        )
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": executor_id})


def find_for_running_executors(conn):
    query1 = (
        select(
            func.json_extract(tables["agent_runs"].c["summary_json"], sql_constant("$.executor_id")),
            func.json_extract(
                tables["agent_runs"].c["summary_json"],
                sql_constant("$.executor_identity"),
            ),
        )
        .select_from(tables["agent_runs"])
        .where(
            and_(
                (tables["agent_runs"].c["status"] == sql_constant("running")),
                (
                    func.json_type(
                        tables["agent_runs"].c["summary_json"],
                        sql_constant("$.executor_identity"),
                    )
                    == sql_constant("object")
                ),
            )
        )
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_summary_json_by_run_id(conn, value_1):
    query1 = (
        select(resolved_summary(conn, tables["agent_runs"]).label("summary_json"))
        .select_from(tables["agent_runs"])
        .where((tables["agent_runs"].c["run_id"] == bindparam("p0")))
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1})


def find_for_control_failure_snapshot(conn, limit):
    query1 = (
        select(
            tables["agent_runs"].c["run_id"],
            tables["agent_runs"].c["status"],
            tables["agent_runs"].c["error_json"],
        )
        .select_from(tables["agent_runs"])
        .where(tables["agent_runs"].c["status"].in_([sql_constant("completed"), sql_constant("failed")]))
        .order_by(
            func.coalesce(
                tables["agent_runs"].c["finished_at"],
                tables["agent_runs"].c["started_at"],
            ).desc(),
            tables["agent_runs"].c["run_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": limit})


def find_for_select_batch(conn, success_cutoff, failure_cutoff, batch_size):
    table1 = tables["agent_runs"].alias("r")
    table3 = tables["wakeups"].alias("w")
    query4 = (
        select(sql_constant(1))
        .select_from(table3)
        .where((table3.c["source_run_id"] == table1.c["run_id"]))
        .correlate_except(table3)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table5 = tables["wakeups"].alias("w")
    query6 = (
        select(sql_constant(1))
        .select_from(table5)
        .where((table5.c["consumed_run_id"] == table1.c["run_id"]))
        .correlate_except(table5)
        .set_label_style(LABEL_STYLE_NONE)
    )
    table7 = tables["leases"].alias("l")
    query8 = (
        select(sql_constant(1))
        .select_from(table7)
        .where(
            and_(
                (table7.c["owner_id"] == table1.c["run_id"]),
                (table7.c["status"] == sql_constant("active")),
            )
        )
        .correlate_except(table7)
        .set_label_style(LABEL_STYLE_NONE)
    )
    query9 = (
        select(tables["agent_runs"].c["run_id"])
        .select_from(tables["agent_runs"])
        .order_by(
            tables["agent_runs"].c["started_at"].desc(),
            tables["agent_runs"].c["run_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query2 = (
        select(table1.c["run_id"])
        .select_from(table1)
        .where(
            and_(
                or_(
                    and_(
                        table1.c["status"].in_([sql_constant("completed"), sql_constant("skipped")]),
                        func.julianday(table1.c["finished_at"]) < func.julianday(bindparam("p0")),
                    ),
                    and_(
                        table1.c["status"] == sql_constant("failed"),
                        func.julianday(table1.c["finished_at"]) < func.julianday(bindparam("p1")),
                    ),
                ),
                not_(query4.exists()),
                not_(query6.exists()),
                not_(query8.exists()),
                table1.c["run_id"] != query9.scalar_subquery(),
            )
        )
        .order_by(func.julianday(table1.c["finished_at"]).asc(), table1.c["run_id"].asc())
        .limit(bindparam("p2"))
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": success_cutoff, "p1": failure_cutoff, "p2": batch_size})


def find_for_build_dashboard_data(conn, history_limit):
    query1 = (
        select(
            tables["agent_runs"].c["run_id"],
            tables["agent_runs"].c["status"],
            tables["agent_runs"].c["started_at"],
            tables["agent_runs"].c["finished_at"],
            tables["agent_runs"].c["config_path"],
            tables["agent_runs"].c["dry_run"],
            resolved_summary(conn, tables["agent_runs"]).label("summary_json"),
            tables["agent_runs"].c["error_json"],
        )
        .select_from(tables["agent_runs"])
        .order_by(
            tables["agent_runs"].c["started_at"].desc(),
            tables["agent_runs"].c["run_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": history_limit})


def find_for_latest_run_id(conn):
    query1 = (
        select(tables["agent_runs"].c["run_id"])
        .select_from(tables["agent_runs"])
        .order_by(
            tables["agent_runs"].c["started_at"].desc(),
            tables["agent_runs"].c["run_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_build_run(conn, run_id):
    query1 = (
        select(
            tables["agent_runs"].c["run_id"],
            tables["agent_runs"].c["status"],
            tables["agent_runs"].c["started_at"],
            tables["agent_runs"].c["finished_at"],
            tables["agent_runs"].c["config_path"],
            tables["agent_runs"].c["dry_run"],
            resolved_summary(conn, tables["agent_runs"]).label("summary_json"),
            tables["agent_runs"].c["error_json"],
        )
        .select_from(tables["agent_runs"])
        .where((tables["agent_runs"].c["run_id"] == bindparam("p0")))
        .correlate_except(tables["agent_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": run_id})

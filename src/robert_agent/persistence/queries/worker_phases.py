"""Shared queries operations for worker_phases."""

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, case, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_latest_worker_phases(conn, attempt_id, limit):
    query1 = (
        select(
            tables["worker_phases"].c["phase"],
            tables["worker_phases"].c["status"],
            tables["worker_phases"].c["summary"],
            tables["worker_phases"].c["next_step"],
            tables["worker_phases"].c["created_at"],
        )
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["worker_phases"].c["created_at"].desc(),
            tables["worker_phases"].c["phase_id"].desc(),
        )
        .limit(bindparam("p1"))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id, "p1": limit})


def find_for_backfill_command_phases(conn, attempt_id, phase, status, summary, finished_at):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["worker_phases"])
        .where(
            and_(
                tables["worker_phases"].c["attempt_id"] == bindparam("p0"),
                tables["worker_phases"].c["phase"] == bindparam("p1"),
                tables["worker_phases"].c["status"] == bindparam("p2"),
                tables["worker_phases"].c["summary"] == bindparam("p3"),
                tables["worker_phases"].c["created_at"] >= bindparam("p4"),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn,
        query1,
        {"p0": attempt_id, "p1": phase, "p2": status, "p3": summary, "p4": finished_at},
    )


def find_for_supervise_running_attempts(conn, attempt_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .limit(sql_constant(1))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_phase_and_status_and_summary_and_next_step_by_attempt_id(conn, attempt_id):
    query1 = (
        select(
            tables["worker_phases"].c["phase"],
            tables["worker_phases"].c["status"],
            tables["worker_phases"].c["summary"],
            tables["worker_phases"].c["next_step"],
        )
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["worker_phases"].c["created_at"].desc(),
            tables["worker_phases"].c["phase_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


@cache
def _find_for_attempt_summary_statement():
    query1 = (
        select(
            tables["worker_phases"].c["phase"],
            tables["worker_phases"].c["status"],
            tables["worker_phases"].c["summary"],
            tables["worker_phases"].c["next_step"],
            tables["worker_phases"].c["created_at"],
        )
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["worker_phases"].c["created_at"].desc(),
            tables["worker_phases"].c["phase_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def find_for_attempt_summary(conn, attempt_id):
    return statements.read(conn, _find_for_attempt_summary_statement(), {"p0": attempt_id})


def find_record_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["worker_phases"])
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["worker_phases"].c["created_at"].asc(),
            tables["worker_phases"].c["phase_id"].asc(),
        )
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_build_attempt(conn, attempt_id):
    query1 = (
        select(
            tables["worker_phases"].c["phase"],
            tables["worker_phases"].c["status"],
            tables["worker_phases"].c["summary"],
            tables["worker_phases"].c["next_step"],
            tables["worker_phases"].c["created_at"],
        )
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["worker_phases"].c["created_at"].desc(),
            tables["worker_phases"].c["phase_id"].desc(),
        )
        .limit(sql_constant(10))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_snapshot(conn, attempt_id):
    query1 = (
        select(
            tables["worker_phases"].c["phase"],
            tables["worker_phases"].c["status"],
            tables["worker_phases"].c["created_at"],
            case(
                (
                    (func.length(tables["worker_phases"].c["summary"]) <= sql_constant(4000)),
                    tables["worker_phases"].c["summary"],
                )
            ).label("summary"),
        )
        .select_from(tables["worker_phases"])
        .where((tables["worker_phases"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["worker_phases"].c["created_at"].desc(),
            tables["worker_phases"].c["phase_id"].desc(),
        )
        .limit(sql_constant(3))
        .correlate_except(tables["worker_phases"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})

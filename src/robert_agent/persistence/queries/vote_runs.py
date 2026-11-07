"""Shared queries operations for vote_runs."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    literal_column,
    select,
    bindparam,
    and_,
    func,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_record_by_round_id(conn, round_id):
    query1 = (
        select(literal_column("*"))
        .select_from(tables["vote_runs"])
        .where((tables["vote_runs"].c["round_id"] == bindparam("p0")))
        .order_by(
            tables["vote_runs"].c["created_at"].asc(),
            tables["vote_runs"].c["run_id"].asc(),
        )
        .correlate_except(tables["vote_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": round_id})


def find_for_reserved_runs(conn):
    query1 = (
        select(tables["vote_runs"])
        .select_from(tables["vote_runs"])
        .where((tables["vote_runs"].c["slot_reserved"] == sql_constant(1)))
        .correlate_except(tables["vote_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_new_run(conn, round_id, stage, worker_id):
    query1 = (
        select((func.coalesce(func.max(tables["vote_runs"].c["attempt_no"]), sql_constant(0)) + sql_constant(1)))
        .select_from(tables["vote_runs"])
        .where(
            and_(
                tables["vote_runs"].c["round_id"] == bindparam("p0"),
                tables["vote_runs"].c["stage"] == bindparam("p1"),
                tables["vote_runs"].c["worker_id"] == bindparam("p2"),
            )
        )
        .correlate_except(tables["vote_runs"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": round_id, "p1": stage, "p2": worker_id})

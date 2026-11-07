"""Shared queries operations for daemon_events."""

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
from robert_agent.persistence import statements


def find_for_latest_daemon_summary(conn):
    query1 = (
        select(
            tables["daemon_events"].c["daemon_event_id"],
            tables["daemon_events"].c["daemon_run_id"],
            tables["daemon_events"].c["event_type"],
            tables["daemon_events"].c["status"],
            tables["daemon_events"].c["created_at"],
            tables["daemon_events"].c["metadata_json"],
        )
        .select_from(tables["daemon_events"])
        .order_by(
            tables["daemon_events"].c["created_at"].desc(),
            tables["daemon_events"].c["daemon_event_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["daemon_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def find_for_latest_daemon_summary_2(conn, event_limit):
    query1 = (
        select(
            tables["daemon_events"].c["daemon_event_id"],
            tables["daemon_events"].c["daemon_run_id"],
            tables["daemon_events"].c["event_type"],
            tables["daemon_events"].c["status"],
            tables["daemon_events"].c["created_at"],
            tables["daemon_events"].c["metadata_json"],
        )
        .select_from(tables["daemon_events"])
        .order_by(
            tables["daemon_events"].c["created_at"].desc(),
            tables["daemon_events"].c["daemon_event_id"].desc(),
        )
        .limit(bindparam("p0"))
        .correlate_except(tables["daemon_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": event_limit})


def find_for_control_failure_snapshot(conn, lane, limit):
    query1 = (
        select(
            tables["daemon_events"].c["daemon_event_id"],
            tables["daemon_events"].c["status"],
        )
        .select_from(tables["daemon_events"])
        .where(
            and_(
                (
                    or_(
                        (
                            and_(
                                (tables["daemon_events"].c["status"] == sql_constant("failed")),
                                (
                                    or_(
                                        (
                                            func.json_type(
                                                tables["daemon_events"].c["metadata_json"],
                                                sql_constant("$.control_lane"),
                                            )
                                            == sql_constant("text")
                                        ),
                                        tables["daemon_events"]
                                        .c["event_type"]
                                        .in_(
                                            [
                                                sql_constant("child_failed"),
                                                sql_constant("child_timeout"),
                                                sql_constant("executor_interrupted"),
                                                sql_constant("partial_failure"),
                                                sql_constant("repository_pipeline_failed"),
                                            ]
                                        ),
                                    )
                                ),
                            )
                        ),
                        tables["daemon_events"]
                        .c["event_type"]
                        .in_(
                            [
                                sql_constant("live_poll_completed"),
                                sql_constant("local_drain_completed"),
                                sql_constant("scan_completed"),
                                sql_constant("publish_completed"),
                            ]
                        ),
                    )
                ),
                (
                    func.coalesce(
                        func.json_extract(
                            tables["daemon_events"].c["metadata_json"],
                            sql_constant("$.control_lane"),
                        ),
                        case(
                            (
                                (
                                    func.json_extract(
                                        tables["daemon_events"].c["metadata_json"],
                                        sql_constant("$.context_refresh"),
                                    )
                                    == sql_constant(1)
                                ),
                                sql_constant("context_refresh"),
                            ),
                            (
                                (tables["daemon_events"].c["event_type"] == sql_constant("local_drain_completed")),
                                sql_constant("local"),
                            ),
                            (
                                (tables["daemon_events"].c["event_type"] == sql_constant("publish_completed")),
                                sql_constant("publish"),
                            ),
                            else_=sql_constant("scan"),
                        ),
                    )
                    == bindparam("p0")
                ),
            )
        )
        .order_by(
            tables["daemon_events"].c["created_at"].desc(),
            tables["daemon_events"].c["daemon_event_id"].desc(),
        )
        .limit(bindparam("p1"))
        .correlate_except(tables["daemon_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": lane, "p1": limit})


def find_for_select_batch(conn, event_cutoff, value_2, batch_size):
    query1 = (
        select(tables["daemon_events"].c["daemon_event_id"])
        .select_from(tables["daemon_events"])
        .where(
            or_(
                (
                    and_(
                        tables["daemon_events"]
                        .c["status"]
                        .in_(
                            [
                                sql_constant("ok"),
                                sql_constant("running"),
                                sql_constant("skipped"),
                            ]
                        ),
                        (func.julianday(tables["daemon_events"].c["created_at"]) < func.julianday(bindparam("p0"))),
                    )
                ),
                (
                    and_(
                        not_(
                            tables["daemon_events"]
                            .c["status"]
                            .in_(
                                [
                                    sql_constant("ok"),
                                    sql_constant("running"),
                                    sql_constant("skipped"),
                                ]
                            )
                        ),
                        (func.julianday(tables["daemon_events"].c["created_at"]) < func.julianday(bindparam("p1"))),
                    )
                ),
            )
        )
        .order_by(
            func.julianday(tables["daemon_events"].c["created_at"]).asc(),
            tables["daemon_events"].c["daemon_event_id"].asc(),
        )
        .limit(bindparam("p2"))
        .correlate_except(tables["daemon_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": event_cutoff, "p1": value_2, "p2": batch_size})


def find_for_controller_facts(conn, run_id, lane):
    query1 = (
        select(
            tables["daemon_events"].c["daemon_event_id"],
            func.json_extract(
                tables["daemon_events"].c["metadata_json"],
                sql_constant("$.cause_status"),
            ).label("cause"),
        )
        .select_from(tables["daemon_events"])
        .where(
            and_(
                tables["daemon_events"].c["daemon_run_id"] == bindparam("p0"),
                tables["daemon_events"].c["event_type"] == sql_constant("control_plane_attention"),
                func.json_extract(
                    tables["daemon_events"].c["metadata_json"],
                    sql_constant("$.control_lane"),
                )
                == bindparam("p1"),
            )
        )
        .order_by(
            tables["daemon_events"].c["created_at"].desc(),
            tables["daemon_events"].c["daemon_event_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["daemon_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": run_id, "p1": lane})


def controller_facts_filtered(conn, items, value_1):
    query1 = (
        select(
            tables["daemon_events"].c["daemon_event_id"],
            tables["daemon_events"].c["status"],
            tables["daemon_events"].c["created_at"],
        )
        .select_from(tables["daemon_events"])
        .where(
            and_(
                (tables["daemon_events"].c["daemon_run_id"] == bindparam("p0")),
                tables["daemon_events"].c["event_type"].in_(bindparam("items", expanding=True)),
            )
        )
        .order_by(
            tables["daemon_events"].c["created_at"].desc(),
            tables["daemon_events"].c["daemon_event_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["daemon_events"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "items": items})

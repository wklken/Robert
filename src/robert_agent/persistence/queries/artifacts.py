"""Shared queries operations for artifacts."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    and_,
    or_,
    not_,
    null,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_latest_artifacts(conn, task_id):
    query1 = (
        select(tables["artifacts"].c["artifact_type"], tables["artifacts"].c["path"])
        .select_from(tables["artifacts"])
        .where(
            and_(
                (tables["artifacts"].c["task_id"] == bindparam("p0")),
                tables["artifacts"]
                .c["artifact_type"]
                .in_(
                    [
                        sql_constant("prompt"),
                        sql_constant("worker_stdout"),
                        sql_constant("worker_stderr"),
                    ]
                ),
            )
        )
        .order_by(
            tables["artifacts"].c["created_at"].asc(),
            tables["artifacts"].c["artifact_id"].asc(),
        )
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_for_prompt_path_for_task(conn, task_id):
    query1 = (
        select(tables["artifacts"].c["path"])
        .select_from(tables["artifacts"])
        .where(
            and_(
                (tables["artifacts"].c["task_id"] == bindparam("p0")),
                (tables["artifacts"].c["artifact_type"] == sql_constant("prompt")),
            )
        )
        .order_by(
            tables["artifacts"].c["created_at"].desc(),
            tables["artifacts"].c["artifact_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id})


def find_path_by_attempt_id(conn, attempt_id):
    query1 = (
        select(tables["artifacts"].c["path"])
        .select_from(tables["artifacts"])
        .where((tables["artifacts"].c["attempt_id"] == bindparam("p0")))
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_attempt_log_tails(conn, attempt_id):
    query1 = (
        select(tables["artifacts"].c["artifact_type"], tables["artifacts"].c["path"])
        .select_from(tables["artifacts"])
        .where(
            and_(
                (tables["artifacts"].c["attempt_id"] == bindparam("p0")),
                tables["artifacts"]
                .c["artifact_type"]
                .in_([sql_constant("worker_stdout"), sql_constant("worker_stderr")]),
            )
        )
        .order_by(
            tables["artifacts"].c["created_at"].desc(),
            tables["artifacts"].c["artifact_id"].desc(),
        )
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def load_artifact_preview(conn, task_id, artifact_type, artifact_id, artifact_id_4):
    query1 = (
        select(
            tables["artifacts"].c["artifact_type"],
            tables["artifacts"].c["path"],
            tables["artifacts"].c["bytes"],
        )
        .select_from(tables["artifacts"])
        .where(
            and_(
                tables["artifacts"].c["task_id"] == bindparam("p0"),
                tables["artifacts"].c["artifact_type"] == bindparam("p1"),
                or_(
                    bindparam("p2").is_(null()),
                    tables["artifacts"].c["artifact_id"] == bindparam("p3"),
                ),
            )
        )
        .order_by(
            tables["artifacts"].c["created_at"].desc(),
            tables["artifacts"].c["artifact_id"].desc(),
        )
        .limit(sql_constant(1))
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn,
        query1,
        {"p0": task_id, "p1": artifact_type, "p2": artifact_id, "p3": artifact_id_4},
    )


def find_artifact_id_and_artifact_type_and_path_and_bytes_by_attempt_id(conn, attempt_id):
    query1 = (
        select(
            tables["artifacts"].c["artifact_id"],
            tables["artifacts"].c["artifact_type"],
            tables["artifacts"].c["path"],
            tables["artifacts"].c["bytes"],
        )
        .select_from(tables["artifacts"])
        .where((tables["artifacts"].c["attempt_id"] == bindparam("p0")))
        .order_by(
            tables["artifacts"].c["created_at"].asc(),
            tables["artifacts"].c["artifact_id"].asc(),
        )
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": attempt_id})


def find_for_milestones(conn, task_id, attempt_id):
    query1 = (
        select(
            tables["artifacts"].c["artifact_type"],
            tables["artifacts"].c["sha256"],
            tables["artifacts"].c["created_at"],
        )
        .select_from(tables["artifacts"])
        .where(
            and_(
                tables["artifacts"].c["task_id"] == bindparam("p0"),
                tables["artifacts"].c["attempt_id"] == bindparam("p1"),
                or_(
                    tables["artifacts"].c["artifact_type"].like(sql_constant("review_%")),
                    tables["artifacts"].c["artifact_type"].like(sql_constant("vote_%")),
                ),
                not_(tables["artifacts"].c["sha256"].is_(null())),
            )
        )
        .order_by(
            tables["artifacts"].c["created_at"].desc(),
            tables["artifacts"].c["artifact_id"].desc(),
        )
        .limit(sql_constant(65))
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": attempt_id})


def find_for_dispatch_facts(conn, task_id, attempt_id):
    query1 = (
        select(sql_constant(1))
        .select_from(tables["artifacts"])
        .where(
            and_(
                tables["artifacts"].c["task_id"] == bindparam("p0"),
                tables["artifacts"].c["artifact_type"] == sql_constant("prompt"),
                or_(
                    tables["artifacts"].c["attempt_id"] == bindparam("p1"),
                    tables["artifacts"].c["attempt_id"].is_(null()),
                ),
            )
        )
        .limit(sql_constant(1))
        .correlate_except(tables["artifacts"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": task_id, "p1": attempt_id})

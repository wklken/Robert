"""Shared queries operations for github_scan_batches."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, null, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_manage_batches(conn, repo_id):
    query1 = (
        select(
            tables["github_scan_batches"].c["batch_id"],
            tables["github_scan_batches"].c["created_at"],
            tables["github_scan_batches"].c["retry_at"],
            tables["github_scan_batches"].c["retry_count"],
            tables["github_scan_batches"].c["attention_reason"],
        )
        .select_from(tables["github_scan_batches"])
        .where(
            and_(
                (tables["github_scan_batches"].c["repo_id"] == bindparam("p0")),
                tables["github_scan_batches"].c["completed_at"].is_(null()),
            )
        )
        .order_by(
            tables["github_scan_batches"].c["created_at"].asc(),
            tables["github_scan_batches"].c["batch_id"].asc(),
        )
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_scan_repo(conn, repo_id):
    query1 = (
        select(
            tables["github_scan_batches"].c["batch_id"],
            tables["github_scan_batches"].c["context_digest"],
            tables["github_scan_batches"].c["raw_events_json"],
            tables["github_scan_batches"].c["retry_at"],
        )
        .select_from(tables["github_scan_batches"])
        .where(
            and_(
                tables["github_scan_batches"].c["repo_id"] == bindparam("p0"),
                tables["github_scan_batches"].c["completed_at"].is_(null()),
                tables["github_scan_batches"].c["attention_reason"].is_(null()),
            )
        )
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_fail_batch(conn, batch_id):
    query1 = (
        select(
            tables["github_scan_batches"].c["repo_id"],
            tables["github_scan_batches"].c["retry_count"],
            tables["github_scan_batches"].c["attention_reason"],
        )
        .select_from(tables["github_scan_batches"])
        .where(
            and_(
                (tables["github_scan_batches"].c["batch_id"] == bindparam("p0")),
                tables["github_scan_batches"].c["completed_at"].is_(null()),
            )
        )
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": batch_id})


def find_for_save_batch(conn, repo_id, context, value_3):
    query1 = (
        select(
            tables["github_scan_batches"].c["batch_id"],
            tables["github_scan_batches"].c["context_digest"],
            tables["github_scan_batches"].c["raw_events_json"],
            tables["github_scan_batches"].c["attention_reason"],
        )
        .select_from(tables["github_scan_batches"])
        .where(
            and_(
                tables["github_scan_batches"].c["repo_id"] == bindparam("p0"),
                tables["github_scan_batches"].c["context_digest"] == bindparam("p1"),
                tables["github_scan_batches"].c["raw_events_json"] == bindparam("p2"),
                tables["github_scan_batches"].c["completed_at"].is_(null()),
            )
        )
        .order_by(tables["github_scan_batches"].c["created_at"].desc())
        .limit(sql_constant(1))
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": context, "p2": value_3})


def find_for_retry_batch(conn, batch_id):
    query1 = (
        select(
            tables["github_scan_batches"].c["repo_id"],
            tables["github_scan_batches"].c["attention_reason"],
        )
        .select_from(tables["github_scan_batches"])
        .where(
            and_(
                (tables["github_scan_batches"].c["batch_id"] == bindparam("p0")),
                tables["github_scan_batches"].c["completed_at"].is_(null()),
            )
        )
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": batch_id})


def find_for_finish_batch(conn, batch_id):
    query1 = (
        select(
            tables["github_scan_batches"].c["repo_id"],
            tables["github_scan_batches"].c["context_digest"],
            tables["github_scan_batches"].c["created_at"],
            tables["github_scan_batches"].c["complete_sources_json"],
            tables["github_scan_batches"].c["raw_events_json"],
        )
        .select_from(tables["github_scan_batches"])
        .where((tables["github_scan_batches"].c["batch_id"] == bindparam("p0")))
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": batch_id})


def find_for_finish_batch_2(conn, value_1, value_2, value_3):
    query1 = (
        select(
            tables["github_scan_batches"].c["batch_id"],
            tables["github_scan_batches"].c["raw_events_json"],
        )
        .select_from(tables["github_scan_batches"])
        .where(
            and_(
                tables["github_scan_batches"].c["repo_id"] == bindparam("p0"),
                tables["github_scan_batches"].c["context_digest"] != bindparam("p1"),
                tables["github_scan_batches"].c["created_at"] <= bindparam("p2"),
                tables["github_scan_batches"].c["completed_at"].is_(null()),
            )
        )
        .correlate_except(tables["github_scan_batches"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "p1": value_2, "p2": value_3})


def due_batch(conn, repo_id, now, context=None):
    batch = tables["github_scan_batches"]
    stmt = select(batch).where(
        batch.c.repo_id == repo_id,
        batch.c.completed_at.is_(None),
        batch.c.attention_reason.is_(None),
        batch.c.retry_at <= now,
    )
    if context is not None:
        stmt = stmt.where(batch.c.context_digest == context)
    return statements.read(conn, stmt.order_by(batch.c.created_at, batch.c.batch_id).limit(1))

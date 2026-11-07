"""Shared queries operations for github_scan_reads."""

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_source_read(conn, repo_id, key, value_3):
    query1 = (
        select(tables["github_scan_reads"].c["response_json"])
        .select_from(tables["github_scan_reads"])
        .where(
            and_(
                tables["github_scan_reads"].c["repo_id"] == bindparam("p0"),
                tables["github_scan_reads"].c["command_key"] == bindparam("p1"),
                tables["github_scan_reads"].c["observed_at"] >= bindparam("p2"),
            )
        )
        .correlate_except(tables["github_scan_reads"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": key, "p2": value_3})


def find_for_next_batch(conn, repo_id, value_2):
    query1 = (
        select(
            tables["github_scan_reads"].c["command_key"],
            tables["github_scan_reads"].c["response_json"],
        )
        .select_from(tables["github_scan_reads"])
        .where(
            and_(
                (tables["github_scan_reads"].c["repo_id"] == bindparam("p0")),
                (tables["github_scan_reads"].c["observed_at"] >= bindparam("p1")),
            )
        )
        .correlate_except(tables["github_scan_reads"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": value_2})

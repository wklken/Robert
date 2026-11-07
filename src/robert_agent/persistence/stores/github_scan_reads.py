"""Shared stores operations for github_scan_reads."""

from sqlalchemy import delete, bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_save_reads(conn, repo_id, key, payload, when):
    stmt = insert(tables["github_scan_reads"]).values(
        {
            "repo_id": bindparam("p0"),
            "command_key": bindparam("p1"),
            "response_json": bindparam("p2"),
            "observed_at": bindparam("p3"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["repo_id", "command_key"],
        set_={
            "response_json": stmt.excluded["response_json"],
            "observed_at": stmt.excluded["observed_at"],
        },
    )
    return statements.write(conn, stmt, {"p0": repo_id, "p1": key, "p2": payload, "p3": when})


def delete_for_finish_batch(conn, value_1):
    stmt = delete(tables["github_scan_reads"]).where((tables["github_scan_reads"].c["observed_at"] < bindparam("p0")))
    return statements.write(conn, stmt, {"p0": value_1})

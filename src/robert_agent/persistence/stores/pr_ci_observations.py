"""Shared stores operations for pr_ci_observations."""

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_store_ci_observations(
    conn,
    value_1,
    episode_id,
    source_kind,
    external_id,
    attempt_no,
    value_6,
    value_7,
    value_8,
    value_9,
    value_10,
    value_11,
    value_12,
):
    stmt = insert(tables["pr_ci_observations"]).values(
        {
            "observation_id": bindparam("p0"),
            "episode_id": bindparam("p1"),
            "source_kind": bindparam("p2"),
            "external_id": bindparam("p3"),
            "attempt_no": bindparam("p4"),
            "check_name": bindparam("p5"),
            "status": bindparam("p6"),
            "conclusion": bindparam("p7"),
            "details_url": bindparam("p8"),
            "completed_at": bindparam("p9"),
            "failure_signature": bindparam("p10"),
            "metadata_json": bindparam("p11"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_kind", "external_id", "attempt_no"],
        set_={
            "episode_id": stmt.excluded["episode_id"],
            "check_name": stmt.excluded["check_name"],
            "status": stmt.excluded["status"],
            "conclusion": stmt.excluded["conclusion"],
            "details_url": stmt.excluded["details_url"],
            "completed_at": stmt.excluded["completed_at"],
            "failure_signature": stmt.excluded["failure_signature"],
            "metadata_json": stmt.excluded["metadata_json"],
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": episode_id,
            "p2": source_kind,
            "p3": external_id,
            "p4": attempt_no,
            "p5": value_6,
            "p6": value_7,
            "p7": value_8,
            "p8": value_9,
            "p9": value_10,
            "p10": value_11,
            "p11": value_12,
        },
    )

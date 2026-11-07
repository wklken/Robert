"""Shared stores operations for pr_remediation_episodes."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, not_, null, case, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def update_for_cancel_execution(conn, now, subject_id):
    stmt = (
        update(tables["pr_remediation_episodes"])
        .values({"status": sql_constant("canceled"), "updated_at": bindparam("p0")})
        .where(
            and_(
                (tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p1")),
                not_(
                    tables["pr_remediation_episodes"]
                    .c["status"]
                    .in_(
                        [
                            sql_constant("resolved"),
                            sql_constant("exhausted"),
                            sql_constant("superseded"),
                            sql_constant("canceled"),
                        ]
                    )
                ),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": subject_id})


def update_for_transition(conn, new_status, now, terminal_at, value_4, episode_id, old_status):
    stmt = (
        update(tables["pr_remediation_episodes"])
        .values(
            {
                "status": bindparam("p0"),
                "updated_at": bindparam("p1"),
                "terminal_at": bindparam("p2"),
                "metadata_json": bindparam("p3"),
            }
        )
        .where(
            and_(
                (tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p4")),
                (tables["pr_remediation_episodes"].c["status"] == bindparam("p5")),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": new_status,
            "p1": now,
            "p2": terminal_at,
            "p3": value_4,
            "p4": episode_id,
            "p5": old_status,
        },
    )


def insert_for_create_episode(
    conn,
    episode_id,
    workstream_id,
    episode_kind,
    subject_key,
    head_sha,
    base_sha,
    failure_signature_value,
    now,
    now_9,
    value_10,
):
    stmt = insert(tables["pr_remediation_episodes"]).values(
        {
            "episode_id": bindparam("p0"),
            "workstream_id": bindparam("p1"),
            "episode_kind": bindparam("p2"),
            "subject_key": bindparam("p3"),
            "observed_head_sha": bindparam("p4"),
            "observed_base_sha": bindparam("p5"),
            "status": sql_constant("open"),
            "failure_signature": bindparam("p6"),
            "first_seen_at": bindparam("p7"),
            "updated_at": bindparam("p8"),
            "metadata_json": bindparam("p9"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["workstream_id", "episode_kind", "subject_key"],
        set_={
            "updated_at": stmt.excluded["updated_at"],
            "failure_signature": case(
                (
                    (tables["pr_remediation_episodes"].c["status"] == sql_constant("open")),
                    func.coalesce(
                        stmt.excluded["failure_signature"],
                        tables["pr_remediation_episodes"].c["failure_signature"],
                    ),
                ),
                else_=tables["pr_remediation_episodes"].c["failure_signature"],
            ),
            "metadata_json": case(
                (
                    (tables["pr_remediation_episodes"].c["status"] == sql_constant("open")),
                    stmt.excluded["metadata_json"],
                ),
                else_=tables["pr_remediation_episodes"].c["metadata_json"],
            ),
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": episode_id,
            "p1": workstream_id,
            "p2": episode_kind,
            "p3": subject_key,
            "p4": head_sha,
            "p5": base_sha,
            "p6": failure_signature_value,
            "p7": now,
            "p8": now_9,
            "p9": value_10,
        },
    )


def record_task_started(conn, task_id, now, episode_id):
    stmt = (
        update(tables["pr_remediation_episodes"])
        .values(
            {
                "status": sql_constant("remediating"),
                "attempt_count": (tables["pr_remediation_episodes"].c["attempt_count"] + sql_constant(1)),
                "last_task_id": bindparam("p0"),
                "updated_at": bindparam("p1"),
                "terminal_at": null(),
            }
        )
        .where(
            and_(
                (tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p2")),
                (tables["pr_remediation_episodes"].c["status"] == sql_constant("open")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": task_id, "p1": now, "p2": episode_id})


def record_publish_result(conn, new_status, result_head_sha, now, value_4, episode_id, status):
    stmt = (
        update(tables["pr_remediation_episodes"])
        .values(
            {
                "status": bindparam("p0"),
                "result_head_sha": func.coalesce(
                    bindparam("p1"),
                    tables["pr_remediation_episodes"].c["result_head_sha"],
                ),
                "updated_at": bindparam("p2"),
                "terminal_at": bindparam("p3"),
            }
        )
        .where(
            and_(
                (tables["pr_remediation_episodes"].c["episode_id"] == bindparam("p4")),
                (tables["pr_remediation_episodes"].c["status"] == bindparam("p5")),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": new_status,
            "p1": result_head_sha,
            "p2": now,
            "p3": value_4,
            "p4": episode_id,
            "p5": status,
        },
    )

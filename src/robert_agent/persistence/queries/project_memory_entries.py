"""Shared queries operations for project_memory_entries."""

from sqlalchemy import select, literal_column, bindparam, and_, func, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_propose_candidates(conn, repo_id):
    query1 = (
        select(
            tables["project_memory_entries"].c["memory_id"],
            tables["project_memory_entries"].c["kind"],
            tables["project_memory_entries"].c["title"],
            tables["project_memory_entries"].c["short_summary"],
            tables["project_memory_entries"].c["confidence"],
            tables["project_memory_entries"].c["metadata_json"],
        )
        .select_from(tables["project_memory_entries"])
        .where((tables["project_memory_entries"].c["repo_id"] == bindparam("p0")))
        .order_by(
            tables["project_memory_entries"].c["updated_at"].desc(),
            tables["project_memory_entries"].c["memory_id"].asc(),
        )
        .correlate_except(tables["project_memory_entries"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id})


def find_for_candidate_ids_from_threads(conn, repo_id, thread_key):
    query1 = (
        select(tables["project_memory_entries"].c["memory_id"])
        .select_from(tables["project_memory_entries"])
        .where(
            and_(
                (tables["project_memory_entries"].c["repo_id"] == bindparam("p0")),
                (tables["project_memory_entries"].c["memory_thread_key"] == bindparam("p1")),
            )
        )
        .correlate_except(tables["project_memory_entries"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": thread_key})


def find_for_upsert_entry(conn, repo_id, memory_thread_key, kind, title):
    query1 = (
        select(
            tables["project_memory_entries"].c["memory_id"],
            tables["project_memory_entries"].c["revision_count"],
        )
        .select_from(tables["project_memory_entries"])
        .where(
            and_(
                tables["project_memory_entries"].c["repo_id"] == bindparam("p0"),
                tables["project_memory_entries"].c["memory_thread_key"] == bindparam("p1"),
                tables["project_memory_entries"].c["kind"] == bindparam("p2"),
                tables["project_memory_entries"].c["title"] == bindparam("p3"),
            )
        )
        .correlate_except(tables["project_memory_entries"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": repo_id, "p1": memory_thread_key, "p2": kind, "p3": title})


def find_for_knowledge_proposal_repos(conn):
    query1 = (
        select(
            tables["project_memory_entries"].c["repo_id"],
            func.count(literal_column("*")).label("count"),
        )
        .select_from(tables["project_memory_entries"])
        .group_by(tables["project_memory_entries"].c["repo_id"])
        .correlate_except(tables["project_memory_entries"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})


def retrieve_memories_filtered(conn, items):
    query1 = (
        select(
            tables["project_memory_entries"].c["memory_id"],
            tables["project_memory_entries"].c["memory_thread_key"],
            tables["project_memory_entries"].c["kind"],
            tables["project_memory_entries"].c["title"],
            tables["project_memory_entries"].c["short_summary"],
            tables["project_memory_entries"].c["long_summary"],
            tables["project_memory_entries"].c["confidence"],
            tables["project_memory_entries"].c["updated_at"],
            tables["project_memory_entries"].c["metadata_json"],
            tables["project_memory_entries"].c["source_task_id"],
            tables["project_memory_entries"].c["source_result_id"],
        )
        .select_from(tables["project_memory_entries"])
        .where(tables["project_memory_entries"].c["memory_id"].in_(bindparam("items", expanding=True)))
        .correlate_except(tables["project_memory_entries"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"items": items})


def memory_source(conn, repo_id, task_id, result_id):
    result, task, stream = tables["worker_results"], tables["tasks"], tables["workstreams"]
    return statements.read(
        conn,
        select(result)
        .select_from(
            result.join(task, task.c.task_id == result.c.task_id).join(
                stream, stream.c.workstream_id == task.c.workstream_id
            )
        )
        .where(result.c.result_id == result_id, task.c.task_id == task_id, stream.c.repo_id == repo_id),
    )


def deployment_proof(conn, result_id):
    proof = tables["supervision_checkpoints"]
    return statements.read(
        conn,
        select(proof.c.scope_id, proof.c.metadata_json, proof.c.updated_at)
        .where(
            proof.c.scope_type == "repair",
            proof.c.rule_id == "verification",
            func.json_extract(proof.c.metadata_json, "$.deployed_verified.result_id") == result_id,
            func.json_extract(proof.c.metadata_json, "$.deployed_verified.status") == "verified",
        )
        .order_by(proof.c.updated_at.desc())
        .limit(1),
    )

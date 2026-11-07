"""Count attempts that still own worker capacity."""

from robert_agent.execution import inspection as supervise
from robert_agent.adapters.sqlite import execution_control as execution_store


def running_attempt_count(conn, repo_id):
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id = ?
          AND {supervise.occupied_attempt_sql()}
          AND t.lifecycle IN ('queued', 'running')
        """,
        (repo_id,),
    ).fetchone()[0]


def running_attempt_count_for_repos(conn, repo_ids):
    repo_ids = [repo_id for repo_id in repo_ids if repo_id]
    if not repo_ids:
        return 0
    placeholders = ",".join("?" for _repo_id in repo_ids)
    return conn.execute(
        f"""
        SELECT COUNT(*)
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id IN ({placeholders})
          AND {supervise.occupied_attempt_sql()}
          AND t.lifecycle IN ('queued', 'running')
        """,
        repo_ids,
    ).fetchone()[0]


def capacity_full(conn, repo_id, max_concurrency):
    return running_attempt_count(conn, repo_id) >= max_concurrency


def capacity_full_for_repos(conn, repo_ids, max_concurrency):
    return running_attempt_count_for_repos(conn, repo_ids) >= max_concurrency


def has_occupied_runnable_attempt(conn, repo_ids):
    placeholders = ",".join("?" for _ in repo_ids)
    return bool(
        conn.execute(
            f"""SELECT 1 FROM attempts a JOIN tasks t ON t.task_id=a.task_id
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE w.repo_id IN ({placeholders}) AND t.lifecycle IN ('queued','running')
          AND {supervise.occupied_attempt_sql()} AND {execution_store.runnable_sql()} LIMIT 1""",
            repo_ids,
        ).fetchone()
    )

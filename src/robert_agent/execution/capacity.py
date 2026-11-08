"""Count attempts that still own worker capacity."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts


def running_attempt_count(conn, repo_id):
    return db_records.first(db_read_attempts.find_for_running_attempt_count(conn, repo_id))[0]


def running_attempt_count_for_repos(conn, repo_ids):
    repo_ids = [repo_id for repo_id in repo_ids if repo_id]
    if not repo_ids:
        return 0
    return db_records.first(db_read_attempts.running_attempt_count_for_repos_filtered(conn, repo_ids))[0]


def capacity_full(conn, repo_id, max_concurrency):
    return running_attempt_count(conn, repo_id) >= max_concurrency


def capacity_full_for_repos(conn, repo_ids, max_concurrency):
    return running_attempt_count_for_repos(conn, repo_ids) >= max_concurrency


def has_occupied_runnable_attempt(conn, repo_ids):
    return bool(db_records.first(db_read_attempts.has_occupied_runnable_attempt_filtered(conn, repo_ids)))


def can_resume_native_slot(conn, attempt_id):
    return bool(db_records.first(db_read_attempts.can_resume_native_slot(conn, attempt_id)))

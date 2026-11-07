"""Register repository metadata in the caller-owned transaction."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import repos as db_read_repos
from robert_agent.persistence.stores import repos as db_write_repos


def upsert_repo(conn, repo):
    existing = db_records.first(db_read_repos.find_repo_id_by_full_name(conn, repo["full_name"]))
    repo_id = existing[0] if existing else f"repo:{repo['full_name']}"
    db_write_repos.insert_for_upsert_control_repo(
        conn,
        repo_id,
        repo["full_name"],
        repo["github_account"],
        repo["default_base_branch"],
        repo["repo_root"],
        repo["worktree_root"],
    )
    return repo_id


def lookup_id(conn, full_name, *, fallback=None):
    row = db_records.first(db_read_repos.find_repo_id_by_full_name(conn, full_name))
    return row[0] if row else fallback


def list_ids(conn):
    return [row[0] for row in db_read_repos.list_ids(conn)]

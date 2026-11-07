"""Prepare durable repository controls for a configured local interface."""

from robert_agent.persistence import database as db_database

from robert_agent.persistence.stores import repos as db_write_repos
from contextlib import closing
from robert_agent.persistence.schema import database as storage


def _upsert_control_repo(conn, repo):
    repo_id = f"repo:{repo['full_name']}"
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


def prepare_repositories(config):
    db_path = config["db_path"]
    storage.init_database(db_path)
    with closing(db_database.connect(db_path)) as conn, conn:
        return frozenset(_upsert_control_repo(conn, repo) for repo in config["repos"])

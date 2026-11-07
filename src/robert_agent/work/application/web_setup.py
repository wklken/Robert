"""Prepare durable repository controls for a configured local interface."""
from contextlib import closing
import sqlite3
from robert_agent.adapters.sqlite import database as storage
from robert_agent.adapters.sqlite import execution_control as execution_store


def _upsert_control_repo(conn, repo):
    repo_id = f"repo:{repo['full_name']}"
    conn.execute(
        """
        INSERT INTO repos(
          repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root, enabled
        )
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(repo_id) DO UPDATE SET
          full_name = excluded.full_name,
          github_account = excluded.github_account,
          default_base_branch = excluded.default_base_branch,
          repo_root = excluded.repo_root,
          worktree_root = excluded.worktree_root,
          enabled = excluded.enabled
        """,
        (
            repo_id,
            repo["full_name"],
            repo["github_account"],
            repo["default_base_branch"],
            repo["repo_root"],
            repo["worktree_root"],
        ),
    )
    return repo_id



def prepare_repositories(config):
    db_path = config["db_path"]
    storage.init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        execution_store.require_migrated(conn)
        return frozenset(_upsert_control_repo(conn, repo) for repo in config["repos"])

"""Shared stores operations for repos."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import bindparam
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_fixture(conn):
    stmt = insert(tables["repos"]).values(
        {
            "repo_id": sql_constant("repo-1"),
            "full_name": sql_constant("x/y"),
            "github_account": sql_constant("robot"),
            "default_base_branch": sql_constant("main"),
            "repo_root": sql_constant("/repo"),
            "worktree_root": sql_constant("/repo/.worktrees"),
        }
    )
    return statements.write(conn, stmt, {})


def insert_canary_fixture(conn, repo):
    stmt = insert(tables["repos"]).values(
        {
            "repo_id": sql_constant("repo-production-canary"),
            "full_name": bindparam("p0"),
            "github_account": sql_constant("robert"),
            "default_base_branch": sql_constant("main"),
            "repo_root": sql_constant(""),
            "worktree_root": sql_constant(""),
        }
    )
    return statements.write(conn, stmt, {"p0": repo})


def insert_for_upsert_control_repo(
    conn,
    repo_id,
    full_name,
    github_account,
    default_base_branch,
    repo_root,
    worktree_root,
):
    stmt = insert(tables["repos"]).values(
        {
            "repo_id": bindparam("p0"),
            "full_name": bindparam("p1"),
            "github_account": bindparam("p2"),
            "default_base_branch": bindparam("p3"),
            "repo_root": bindparam("p4"),
            "worktree_root": bindparam("p5"),
            "enabled": sql_constant(1),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["repo_id"],
        set_={
            "full_name": stmt.excluded["full_name"],
            "github_account": stmt.excluded["github_account"],
            "default_base_branch": stmt.excluded["default_base_branch"],
            "repo_root": stmt.excluded["repo_root"],
            "worktree_root": stmt.excluded["worktree_root"],
            "enabled": stmt.excluded["enabled"],
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": repo_id,
            "p1": full_name,
            "p2": github_account,
            "p3": default_base_branch,
            "p4": repo_root,
            "p5": worktree_root,
        },
    )

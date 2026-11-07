"""Register repository metadata in the caller-owned transaction."""


def upsert_repo(conn, repo):
    existing = conn.execute("SELECT repo_id FROM repos WHERE full_name=?", (repo["full_name"],)).fetchone()
    repo_id = existing[0] if existing else f"repo:{repo['full_name']}"
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


def lookup_id(conn, full_name, *, fallback=None):
    row = conn.execute("SELECT repo_id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
    return row[0] if row else fallback


def list_ids(conn):
    return [row[0] for row in conn.execute("SELECT repo_id FROM repos")]

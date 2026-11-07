from contextlib import closing
import sqlite3
from tests.support.temp_workspace import TemporaryWorkspace


class LoopRunnableScopeTests(TemporaryWorkspace):
    def test_loop_engine_local_work_accepts_repo_id_list(self):
        from robert_agent.runtime import loop as loop_engine
        from robert_agent.persistence.schema import database as storage

        data_dir = self.root / "data"
        repo_root = self.root / "repo-a"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        db_path = data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo:a', 'Org/a', 'robot', 'main', ?, ?)",
                (str(repo_root), str(worktree_root)),
            )
            conn.execute(
                "INSERT INTO wakeups(wakeup_id, repo_id, reason, dedupe_key, status, not_before_at, created_at, updated_at) VALUES ('wakeup-a', 'repo:a', 'manual_operator_request', 'manual-a', 'pending', ?, ?, ?)",
                (now, now, now),
            )
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=["repo:a"]))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=["repo:b"]))

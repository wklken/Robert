from contextlib import closing
import sqlite3
from robert_agent.delivery.application import scope as cycle_scope
from tests.support.summary_database import SummaryFixture


class ScopeTests(SummaryFixture):
    def test_audit_scope_records_controller_observed_commit_and_fork(self):
        import subprocess

        db_path = self.root / "scope.sqlite3"
        self._init_summary_db(db_path)
        tree = self.root / "real-tree"
        tree.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=tree, check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "-b", "codex/fix")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        git("commit", "--allow-empty", "-m", "result")
        git("remote", "add", "origin", "https://github.com/expected-owner/y.git")
        expected_sha = git("rev-parse", "HEAD")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET worktree_path = ?, branch_name = 'codex/fix'",
                (str(tree),),
            )
            ws = conn.execute(
                "SELECT workstream_id FROM tasks WHERE task_id='task-1'"
            ).fetchone()[0]
            scope = cycle_scope.for_result(conn, "task-1", "attempt-1", ws)
        self.assertEqual(scope["result_head_sha"], expected_sha)
        self.assertEqual(scope["head_owner"], "expected-owner")

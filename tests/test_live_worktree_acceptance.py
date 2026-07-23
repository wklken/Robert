from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class LiveWorktreeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_repo = self.root / "source-repo"
        self.source_repo.mkdir()
        (self.source_repo / ".git").mkdir()
        self.source_data_dir = self.root / "source-data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""data_dir: {self.source_data_dir}
database: dd.sqlite3
max_concurrency: 1
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: main
    repo_root: {self.source_repo}
    worktree_root: {self.source_repo / ".worktrees"}
""",
            encoding="utf-8",
        )

    def test_live_worktree_acceptance_creates_real_git_worktree_in_isolated_checkout(self):
        from robert_agent import live_worktree_acceptance
        workspace_dir = (self.root / "worktree-acceptance").resolve()

        result = live_worktree_acceptance.live_worktree_acceptance(
            self.config_path,
            workspace_dir=workspace_dir,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route_id"], "new-pr")
        self.assertEqual(result["attempt_status"], "running")
        self.assertEqual(result["branch_name"], "codex/dd-77-fix-worktree-acceptance")
        self.assertTrue(Path(result["worktree_path"]).is_dir())
        self.assertTrue(str(Path(result["worktree_path"])).startswith(str(workspace_dir)))
        self.assertEqual(result["git_branch"], result["branch_name"])
        self.assertTrue(result["git_worktree_list_contains_branch"])
        self.assertFalse((self.source_data_dir / "dd.sqlite3").exists())
        with closing(sqlite3.connect(result["db_path"])) as conn, conn:
            attempt = conn.execute(
                "SELECT status, worktree_path, branch_name FROM attempts"
            ).fetchone()
        self.assertEqual(
            attempt,
            ("running", result["worktree_path"], result["branch_name"]),
        )


if __name__ == "__main__":
    unittest.main()

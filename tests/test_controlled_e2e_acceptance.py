from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class ControlledEndToEndAcceptanceTests(unittest.TestCase):
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
worker_startup_grace_seconds: 1
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

    def test_controlled_e2e_acceptance_completes_new_pr_workflow(self):
        from robert_agent import controlled_e2e_acceptance
        workspace_dir = (self.root / "controlled-e2e").resolve()

        result = controlled_e2e_acceptance.controlled_e2e_acceptance(
            self.config_path,
            workspace_dir=workspace_dir,
            timeout_seconds=5,
            poll_interval_seconds=0.05,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route_id"], "new-pr")
        self.assertEqual(result["task_lifecycle"], "completed")
        self.assertEqual(result["issue_workstream_lifecycle"], "completed")
        self.assertEqual(result["derived_pr_workstream_lifecycle"], "completed")
        self.assertEqual(result["worker_result"]["output_type"], "new_pr")
        self.assertEqual(result["publish_result"]["published_count"], 2)
        self.assertEqual(result["publish_result"]["finalized_task_count"], 1)
        self.assertEqual(result["github_actions"], [("push_existing_pr", "accepted", "published"), ("open_pr", "accepted", "published")])
        self.assertEqual(result["published_pr_url"], "https://github.com/example/repo/pull/42")
        self.assertTrue(Path(result["worktree_path"]).is_dir())
        self.assertFalse((self.source_data_dir / "dd.sqlite3").exists())
        with closing(sqlite3.connect(result["db_path"])) as conn, conn:
            active_tasks = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE lifecycle IN ('queued', 'running')"
            ).fetchone()[0]
        self.assertEqual(active_tasks, 0)


if __name__ == "__main__":
    unittest.main()

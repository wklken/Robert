import json
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class LiveDiscoveryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.worktree_root.mkdir()
        self.data_dir = self.root / "data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""data_dir: {self.data_dir}
database: dd.sqlite3
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: main
    repo_root: {self.repo_root}
    worktree_root: {self.worktree_root}
""",
            encoding="utf-8",
        )

    def test_live_discovery_acceptance_is_read_only_and_reports_gh_commands(self):
        from robert_agent import live_discovery_acceptance
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout
                self.stderr = ""
                self.returncode = 0

        def runner(args, **_kwargs):
            calls.append(args)
            if args[:3] == ["gh", "search", "issues"] and "--assignee" in args:
                return Completed("[]")
            if args[:3] == ["gh", "search", "issues"] and "--mentions" in args:
                return Completed(
                    json.dumps(
                        [
                            {
                                "id": "issue-77",
                                "number": 77,
                                "title": "Need analysis",
                                "body": "@robot please analyze this",
                                "author": {"login": "wklken"},
                                "authorAssociation": "OWNER",
                                "isPullRequest": False,
                                "updatedAt": "2026-06-18T00:00:00Z",
                                "url": "https://github.com/example/repo/issues/77",
                            }
                        ]
                    )
                )
            if args[:3] == ["gh", "api", "notifications"]:
                return Completed("[]")
            raise AssertionError(args)

        result = live_discovery_acceptance.live_discovery_acceptance(
            self.config_path,
            runner=runner,
            limit=5,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["raw_event_count"], 1)
        self.assertEqual(result["normalized_event_count"], 1)
        self.assertEqual(result["sample_events"][0]["event_fingerprint"], "mention:issue-77")
        self.assertEqual(result["sample_events"][0]["mentions_dd"], True)
        self.assertTrue(any("--assignee" in call for call in calls))
        self.assertTrue(any("--mentions" in call for call in calls))
        self.assertTrue(any(call[:3] == ["gh", "api", "notifications"] for call in calls))
        self.assertFalse((self.data_dir / "dd.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()

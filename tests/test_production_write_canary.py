import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class _Completed:
    returncode = 0
    stderr = ""

    def __init__(self, stdout):
        self.stdout = stdout


class _CommentRunner:
    def __init__(self, lookup_stdout, create_stdout=None):
        self.lookup_stdout = lookup_stdout
        self.create_stdout = create_stdout or json.dumps(
            {
                "id": 123,
                "html_url": "https://github.com/x/y/issues/1#issuecomment-123",
            }
        )
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})
        if command == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100", "--paginate", "--slurp"]:
            return _Completed(self.lookup_stdout)
        if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments"]:
            return _Completed(self.create_stdout)
        raise AssertionError(f"unexpected command: {command}")


class ProductionWriteCanaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_canary_plan_mode_never_calls_github(self):
        from robert_agent import production_write_canary
        def fail_if_called(command, **kwargs):
            raise AssertionError(f"unexpected command: {command}")

        result = production_write_canary.production_write_canary(
            target_url="https://github.com/x/y/issues/1",
            workspace_dir=self.root / "canary",
            marker_id="marker-1",
            confirm_github_write=False,
            run_command=fail_if_called,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["write_confirmed"])
        self.assertEqual(result["publish_result"]["status"], "dry_run")
        self.assertEqual(result["publish_result"]["pending_count"], 1)
        self.assertIn("--confirm-github-write", result["next_action"])

    def test_canary_confirmed_write_publishes_comment_through_publisher(self):
        from robert_agent import production_write_canary
        runner = _CommentRunner(lookup_stdout=json.dumps([[]]))

        result = production_write_canary.production_write_canary(
            target_url="https://github.com/x/y/issues/1",
            workspace_dir=self.root / "canary",
            marker_id="marker-2",
            confirm_github_write=True,
            run_command=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "published")
        self.assertTrue(result["write_confirmed"])
        self.assertEqual(result["publish_result"]["published_count"], 1)
        self.assertEqual(result["publish_result"]["deduplicated_count"], 0)
        commands = [call["command"] for call in runner.calls]
        self.assertEqual(commands[0], ["gh", "api", "repos/x/y/issues/1/comments?per_page=100", "--paginate", "--slurp"])
        self.assertEqual(commands[1][:3], ["gh", "api", "repos/x/y/issues/1/comments"])
        with sqlite3.connect(result["db_path"]) as conn:
            row = conn.execute(
                "SELECT publish_status, external_id, target_url, metadata_json FROM github_actions"
            ).fetchone()
        self.assertEqual(row[0], "published")
        self.assertEqual(row[1], "123")
        self.assertEqual(row[2], "https://github.com/x/y/issues/1#issuecomment-123")
        self.assertIn("marker-2", json.loads(row[3])["body"])

    def test_canary_confirmed_write_dedupes_existing_marker(self):
        from robert_agent import production_write_canary
        body = production_write_canary.comment_body("marker-3")
        runner = _CommentRunner(
            lookup_stdout=json.dumps(
                [
                    [
                        {
                            "id": 987,
                            "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                            "body": body,
                        }
                    ]
                ]
            )
        )

        result = production_write_canary.production_write_canary(
            target_url="https://github.com/x/y/issues/1",
            workspace_dir=self.root / "canary",
            marker_id="marker-3",
            confirm_github_write=True,
            run_command=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "deduplicated")
        self.assertEqual(result["publish_result"]["published_count"], 1)
        self.assertEqual(result["publish_result"]["deduplicated_count"], 1)
        commands = [call["command"] for call in runner.calls]
        self.assertEqual(commands, [["gh", "api", "repos/x/y/issues/1/comments?per_page=100", "--paginate", "--slurp"]])

    def test_canary_rejects_non_github_issue_or_pr_url(self):
        from robert_agent import production_write_canary
        result = production_write_canary.production_write_canary(
            target_url="https://example.com/x/y/issues/1",
            workspace_dir=self.root / "canary",
            marker_id="marker-4",
            confirm_github_write=True,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "invalid_target_url")


if __name__ == "__main__":
    unittest.main()

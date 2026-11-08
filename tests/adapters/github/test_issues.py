import json
import subprocess
import unittest
from types import SimpleNamespace

from robert_agent.adapters.github import issues


class IssueDispatchAdapterTests(unittest.TestCase):
    def issue(self, number=12, **extra):
        return {
            "id": number + 100,
            "number": number,
            "html_url": f"https://github.com/example/repo/issues/{number}",
            "user": {"login": "robot"},
            "body": "需求\n<!-- robert-dispatch request_id=dispatch-abc -->",
            **extra,
        }

    def test_create_uses_json_stdin_and_returns_verified_issue(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(stdout=json.dumps(self.issue()), returncode=0)

        result = issues.create("example/repo", title="需求 $(no)", body=self.issue()["body"], runner=runner)
        self.assertEqual(result["number"], 12)
        self.assertEqual(calls[0][0], ["gh", "api", "repos/example/repo/issues", "--method", "POST", "--input", "-"])
        self.assertEqual(json.loads(calls[0][1]["input"])["title"], "需求 $(no)")
        self.assertLessEqual(calls[0][1]["timeout"], 30)

    def test_wrong_identity_prevents_issue_write(self):
        def runner(argv, **kwargs):
            self.assertEqual(argv, ["gh", "api", "user"])
            return SimpleNamespace(stdout='{"login":"another-user"}', returncode=0)

        with self.assertRaisesRegex(ValueError, "identity"):
            issues.check_identity("robot", runner=runner)

    def test_reconciliation_finds_closed_issue_and_ignores_pr_and_wrong_author(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        [
                            self.issue(10, pull_request={}),
                            self.issue(11, user={"login": "other"}),
                            self.issue(12, state="closed"),
                        ]
                    ]
                ),
                returncode=0,
            )

        matches = issues.find_created("example/repo", "dispatch-abc", "robot", "2026-11-08T00:00:00Z", runner=runner)
        self.assertEqual([item["number"] for item in matches], [12])
        self.assertIn("--paginate", calls[0])
        self.assertIn("state=all", calls[0][2])

    def test_bad_remote_metadata_is_not_a_receipt(self):
        for payload in ([], {"number": 12}, self.issue(html_url="https://github.com/other/repo/issues/12")):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                issues.create(
                    "example/repo",
                    title="title",
                    body="body",
                    runner=lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload), returncode=0),
                )

    def test_timeout_propagates_without_retrying_create(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 30)

        with self.assertRaises(subprocess.TimeoutExpired):
            issues.create("example/repo", title="title", body="body", runner=runner)
        self.assertEqual(len(calls), 1)

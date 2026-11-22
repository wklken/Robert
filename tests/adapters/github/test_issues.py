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


class CandidateIssueTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone

        self.since = datetime(2026, 11, 15, tzinfo=timezone.utc)
        self.until = datetime(2026, 11, 22, tzinfo=timezone.utc)
        self.calls = []
        self.responses = []

    def issue(self, number=12, **extra):
        return {
            "id": 100 + number,
            "number": number,
            "html_url": f"https://github.com/example/repo/issues/{number}",
            "user": {"login": "alice"},
            "state": "open",
            "title": "A bug",
            "body": "Details",
            "assignees": [],
            "created_at": "2026-11-20T10:00:00Z",
            **extra,
        }

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(stdout=json.dumps(self.responses.pop(0)), returncode=0)

    def test_recent_reads_open_issues_and_skips_pull_requests_and_outside_window(self):
        self.responses = [
            [
                self.issue(1),
                self.issue(2, pull_request={}),
                self.issue(3, state="closed"),
                self.issue(4, created_at="2026-11-14T10:00:00Z"),
            ]
        ]
        found = issues.list_recent("example/repo", since=self.since, until=self.until, runner=self.runner)
        self.assertEqual([row["number"] for row in found], [1])
        self.assertIn("sort=created", self.calls[0][2])
        self.assertNotIn("--method", self.calls[0])

    def test_missing_assignees_and_invalid_timestamps_are_rejected(self):
        for field, value in (("assignees", None), ("assignees", [{"login": ""}]), ("created_at", "yesterday")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.responses = [[self.issue(**{field: value})]]
                issues.list_recent("example/repo", since=self.since, until=self.until, runner=self.runner)
        row = self.issue()
        del row["assignees"]
        self.responses = [row]
        with self.assertRaises(ValueError):
            issues.get("example/repo", 12, runner=self.runner)

    def test_full_last_page_reports_incomplete_scan(self):
        self.responses = [[self.issue(number) for number in range(1, 101)]]
        with self.assertRaisesRegex(ValueError, "limit"):
            issues.list_recent("example/repo", since=self.since, until=self.until, max_pages=1, runner=self.runner)

    def test_discussion_validates_comments_and_preserves_order(self):
        self.responses = [[{"id": 1, "body": "first"}, {"id": 2, "body": "second"}]]
        self.assertEqual(issues.discussion("example/repo", 12, runner=self.runner), ("first", "second"))
        self.responses = [[{"id": 1, "body": None}]]
        with self.assertRaises(ValueError):
            issues.discussion("example/repo", 12, runner=self.runner)

    def test_get_rejects_another_issue_and_malformed_user(self):
        for row in (self.issue(13), self.issue(user=[])):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.responses = [row]
                issues.get("example/repo", 12, runner=self.runner)

    def test_recent_rejects_naive_window_and_unordered_response(self):
        with self.assertRaises(ValueError):
            issues.list_recent(
                "example/repo", since=self.since.replace(tzinfo=None), until=self.until, runner=self.runner
            )
        self.responses = [[self.issue(created_at="2026-11-19T00:00:00Z"), self.issue(13)]]
        with self.assertRaisesRegex(ValueError, "ordering"):
            issues.list_recent("example/repo", since=self.since, until=self.until, runner=self.runner)

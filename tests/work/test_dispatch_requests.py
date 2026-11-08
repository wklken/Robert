from contextlib import closing
import json
import sqlite3
import subprocess
from types import SimpleNamespace

from robert_agent.work.application import dispatch_requests
from robert_agent.work.errors import WorkItemValidationError, WorkItemNotFoundError


from tests.support.dispatch_requests import DispatchFixture


class DispatchRequestsTests(DispatchFixture):
    def test_draft_has_no_issue_and_submit_is_idempotent(self):
        draft = self.draft()
        self.assertEqual(draft["status"], "draft")
        first = self.command(draft["request_id"])
        self.assertEqual(first, self.command(draft["request_id"]))
        self.assertEqual(first["status"], "accepted")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)
        self.assertNotEqual(self.draft()["request_id"], draft["request_id"])

    def test_wrong_sender_scope_and_disabled_dispatch_cannot_submit(self):
        request_id = self.draft()["request_id"]
        with self.assertRaises(WorkItemNotFoundError):
            self.command(request_id, sender_id="43")
        self.config["messaging"]["dispatch_enabled"] = False
        with self.assertRaises(WorkItemValidationError):
            self.command(request_id)

    def test_create_then_replay_never_posts_twice(self):
        request_id = self.draft()["request_id"]
        self.command(request_id)
        calls = []

        def runner(argv, **kwargs):
            # A separate writer proves no write transaction spans the network.
            with closing(sqlite3.connect(self.db_path, timeout=0)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            calls.append(argv)
            payload = (
                {"login": "robot"}
                if argv[-1] == "user"
                else {
                    "id": 11,
                    "number": 1,
                    "html_url": "https://github.com/example/repo/issues/1",
                    "user": {"login": "robot"},
                    "body": json.loads(kwargs["input"])["body"],
                    "state": "open",
                }
            )
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

        dispatch_requests.advance(self.config, runner=runner)
        dispatch_requests.advance(self.config, runner=runner)
        self.assertEqual(sum("POST" in call for call in calls), 1)
        self.assertEqual(self.command(request_id, "status")["issue_url"], "https://github.com/example/repo/issues/1")

    def test_unknown_post_requires_reconciliation_and_never_reposts(self):
        request_id = self.draft()["request_id"]
        self.command(request_id)
        posts = []

        def runner(argv, **kwargs):
            if argv[-1] == "user":
                return SimpleNamespace(stdout='{"login":"robot"}', returncode=0)
            if "POST" in argv:
                posts.append(argv)
                raise subprocess.TimeoutExpired(argv, 30)
            return SimpleNamespace(stdout="[[]]", returncode=0)

        dispatch_requests.advance(self.config, runner=runner)
        self.assertEqual(self.command(request_id, "status")["status"], "attention")
        self.command(request_id, "retry")
        dispatch_requests.advance(self.config, runner=runner)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.command(request_id, "status")["status"], "attention")

    def test_overlapping_reconciliation_cannot_discard_live_creation_receipt(self):
        request_id = self.draft()["request_id"]
        self.command(request_id)
        posts = []

        def runner(argv, **kwargs):
            if argv[-1] == "user":
                return SimpleNamespace(stdout='{"login":"robot"}', returncode=0)
            if "POST" in argv:
                posts.append(argv)
                dispatch_requests.advance(self.config, runner=runner)
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "id": 11,
                            "number": 1,
                            "html_url": "https://github.com/example/repo/issues/1",
                            "user": {"login": "robot"},
                            "body": json.loads(kwargs["input"])["body"],
                            "state": "open",
                        }
                    ),
                    returncode=0,
                )
            return SimpleNamespace(stdout="[[]]", returncode=0)

        dispatch_requests.advance(self.config, runner=runner)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.command(request_id, "status")["status"], "issue_created")

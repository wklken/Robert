"""Transport failures recover through the publisher without extra model grants."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest import mock

from robert_agent.delivery.application import publish
from robert_agent.delivery import values
from robert_agent.delivery import queries
from robert_agent.readmodels import execution
from tests.support.publication import PublicationFixture


class PublicationTransportRetryTests(PublicationFixture):
    def setUp(self):
        super().setUp()
        self.db = self.root / "publication.sqlite3"
        self._init_summary_db(self.db)
        self.now = datetime.now(timezone.utc)
        self.config = {"repos": [{"full_name": "x/y"}], "supervision": {"mode": "decide"}}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET full_name='x/y'")
            conn.execute(
                "UPDATE tasks SET metadata_json=json_object('trigger_work_item_event_id','operator-request') WHERE task_id='task-1'"
            )
        self.comments = []
        self.writes = 0
        self.reads = 0
        self.responses = []

    def runner(self, command, **kwargs):
        if "?per_page=100" in command[2]:
            self.reads += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.comments), stderr="")
        self.writes += 1
        return self.responses.pop(0)

    def cycle(self, seconds=0):
        self.now += timedelta(seconds=seconds)
        with mock.patch.object(values, "now", return_value=self.now.isoformat()):
            return publish.publish_ready_actions(self.db, run_command=self.runner, config=self.config)

    def metadata(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return json.loads(
                conn.execute("SELECT metadata_json FROM github_actions WHERE action_id='action-1'").fetchone()[0]
            )

    def created(self):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": 987, "html_url": "https://github.com/x/y/issues/1#issuecomment-987"}),
            stderr="",
        )

    def test_network_failure_reconciles_then_retries_in_decide_mode(self):
        self.responses = [
            SimpleNamespace(returncode=1, stdout="", stderr="unexpected end of JSON input"),
            self.created(),
        ]
        failed = self.cycle()
        self.assertEqual(failed["failed_count"], 1)
        self.assertEqual(self.metadata()["publish"]["error_class"], "network_failure")
        self.assertEqual(self.cycle(4)["pending_count"], 0)
        self.assertEqual(self.writes, 1)
        recovered = self.cycle(2)
        self.assertEqual(recovered["published_count"], 1, recovered)
        self.assertEqual(self.writes, 2)
        self.assertGreaterEqual(self.reads, 3)
        self.assertTrue(self.metadata()["publication_diagnostics"])

    def test_ambiguous_success_is_adopted_without_duplicate_write(self):
        self.responses = [SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer")]
        self.cycle()
        self.comments = [
            {
                "id": 987,
                "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                "body": self._dd_comment_body("ready"),
                "user": {"login": "robert-bot"},
            }
        ]
        recovered = self.cycle(6)
        self.assertEqual(recovered["published_count"], 1, recovered)
        self.assertEqual(self.writes, 1)

    def test_truncated_success_response_reconciles_without_duplicate_write(self):
        self.responses = [SimpleNamespace(returncode=0, stdout='{"id":', stderr="")]
        failed = self.cycle()
        self.assertEqual(failed["failed_count"], 1, failed)
        self.comments = [
            {
                "id": 987,
                "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                "body": self._dd_comment_body("ready"),
                "user": {"login": "robert-bot"},
            }
        ]
        recovered = self.cycle(6)
        self.assertEqual(recovered["published_count"], 1, recovered)
        self.assertEqual(self.writes, 1)

    def test_permission_failure_is_recorded_without_automatic_retry(self):
        self.responses = [
            SimpleNamespace(returncode=1, stdout="", stderr="gh: Resource not accessible by integration (HTTP 403)")
        ]
        self.cycle()
        state = self.metadata()["publish"]
        self.assertEqual(state["error_class"], "permission_required")
        self.assertFalse(state["retryable"])
        self.assertTrue(self.metadata()["publication_diagnostics"])
        self.assertEqual(self.cycle(60)["pending_count"], 0)
        self.assertEqual(self.writes, 1)

    def test_network_retries_stop_after_three_writes(self):
        self.responses = [SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer") for _ in range(3)]
        self.cycle()
        self.cycle(6)
        self.cycle(11)
        self.cycle(60)
        state = self.metadata()["publish"]
        self.assertEqual(self.writes, 3)
        self.assertEqual(state["attempt_count"], 3)
        self.assertFalse(state["retryable"])

    def test_rate_limited_reconciliation_keeps_read_and_write_budgets(self):
        self.responses = [SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer"), self.created()]
        self.cycle()
        original = self.runner

        def limited_read(command, **kwargs):
            if "?per_page=100" in command[2]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="< HTTP/2.0 429 Too Many Requests\n< Retry-After: 1800\n\ngh: rate limit exceeded (HTTP 429)",
                )
            return original(command, **kwargs)

        self.runner = limited_read
        self.cycle(6)
        state = self.metadata()["publish"]
        self.assertEqual(state["attempt_count"], 1)
        self.assertTrue(state["reconcile_retry"]["retryable"])
        self.assertEqual(state["reconcile_retry"]["error_class"], "rate_limited")
        self.assertEqual(self.cycle(1799)["pending_count"], 0)
        self.runner = original
        self.assertEqual(self.cycle(2)["published_count"], 1)
        self.assertEqual(self.writes, 2)

    def test_exhausted_reconcile_reads_keep_the_last_provider_cooldown(self):
        self.responses = [SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer")]
        self.cycle()

        def limited_read(command, **kwargs):
            self.assertIn("?per_page=100", command[2])
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="< HTTP/2.0 429 Too Many Requests\n< Retry-After: 120\n\ngh: rate limit exceeded (HTTP 429)",
            )

        self.runner = limited_read
        self.cycle(6)
        self.cycle(120)
        self.cycle(120)
        with closing(sqlite3.connect(self.db)) as conn:
            due = publish.db_read_github_actions.publication_cooldown_until(conn, "robert-bot", self.now.isoformat())
        self.assertEqual(due, (self.now + timedelta(seconds=120)).isoformat())
        self.assertEqual(self.writes, 1)

    def test_primary_rate_limit_waits_for_reset_beyond_ten_minutes(self):
        reset = int((self.now + timedelta(seconds=1800)).timestamp())
        stderr = f'* Request to https://api.github.com/repos/x/y/issues/1/comments\n> POST /repos/x/y/issues/1/comments HTTP/1.1\n> Authorization: token private-credential\n\n< HTTP/2.0 403 Forbidden\n< X-RateLimit-Remaining: 0\n< X-RateLimit-Reset: {reset}\n< X-GitHub-Request-Id: request-123\n< Content-Type: application/json\n\n{{"message":"API rate limit exceeded"}}\n\n* Request took 2ms\ngh: API rate limit exceeded (HTTP 403)\n'
        self.responses = [SimpleNamespace(returncode=1, stdout="", stderr=stderr), self.created()]
        self.cycle()
        state = self.metadata()["publish"]
        self.assertEqual(state["error_class"], "rate_limited")
        due = datetime.fromisoformat(state["next_retry_at"])
        self.assertGreaterEqual(due.timestamp(), reset)
        self.assertEqual(self.cycle(1799)["pending_count"], 0)
        self.assertEqual(self.writes, 1)
        recovered = self.cycle(3)
        self.assertEqual(recovered["published_count"], 1, recovered)
        self.assertEqual(self.writes, 2)
        diagnostic = json.dumps(self.metadata()["publication_diagnostics"])
        self.assertNotIn("private-credential", diagnostic)
        self.assertIn("request-123", diagnostic)

    def test_failed_request_and_response_diagnostics_survive_recovery(self):
        self.responses = [
            SimpleNamespace(returncode=1, stdout="<html>upstream unavailable</html>", stderr="HTTP 502 Bad Gateway"),
            self.created(),
        ]
        self.cycle()
        saved = self.metadata()
        self.assertTrue(saved.get("publication_diagnostics"), saved)
        self.assertIn("upstream unavailable", json.dumps(saved["publication_diagnostics"]))
        self.cycle(6)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            history = execution.publication_history(conn, "task-1")
        self.assertTrue(history[0].get("diagnostics"), history)
        self.assertIn("upstream unavailable", json.dumps(history[0]["diagnostics"]))

    def test_timeout_records_partial_response_and_can_retry(self):
        first = True
        original = self.runner

        def runner(command, **kwargs):
            nonlocal first
            if "?per_page=100" not in command[2] and first:
                first = False
                raise subprocess.TimeoutExpired(command, 60, output="partial-response", stderr="timeout")
            return original(command, **kwargs)

        self.runner = runner
        self.responses = [self.created()]
        self.cycle()
        self.assertIn("partial-response", json.dumps(self.metadata().get("publication_diagnostics")))
        self.assertEqual(self.cycle(6)["published_count"], 1)

    def test_rate_limit_defers_other_actions_using_the_same_account(self):
        self.responses = [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="< HTTP/2.0 429 Too Many Requests\n< Retry-After: 120\n\ngh: rate limit exceeded (HTTP 429)",
            )
        ]
        self.cycle()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO github_actions SELECT 'action-2',result_id,task_id,action_type,target_url,external_id,audit_status,publish_status,created_at,json_object('body',?) FROM github_actions WHERE action_id='action-1'",
                (self._dd_comment_body("second"),),
            )
        self.responses = [self.created()]
        report = self.cycle(60)
        self.assertEqual(self.writes, 1, report)
        self.assertEqual(self.reads, 1, report)

    def test_rate_limit_cooldown_is_shared_across_repositories_of_one_account(self):
        self.responses = [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="< HTTP/2.0 429 Too Many Requests\n< Retry-After: 120\n\ngh: rate limit exceeded (HTTP 429)",
            )
        ]
        self.cycle()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for suffix, account in (("same", "robert-bot"), ("other", "another-bot")):
                conn.execute(
                    "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES (?,?,?,?,?,?)",
                    (suffix, "x/" + suffix, account, "main", "/repo", "/worktrees"),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES (?,?,'active','now','now')",
                    (suffix, suffix),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES (?,?,'running','now','now')",
                    (suffix, suffix),
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at,metadata_json) VALUES (?,?,'comment','accepted','not_published','now','{}')",
                    (suffix, suffix),
                )
            with mock.patch.object(values, "now", return_value=self.now.isoformat()):
                ready = queries.ready_actions(conn, 20)
        self.assertEqual([item["action_id"] for item in ready], ["other"])

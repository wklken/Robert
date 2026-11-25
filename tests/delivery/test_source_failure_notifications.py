"""Source failures retain diagnostics but only sustained failures interrupt operators."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest.mock import Mock, patch

from tests import support  # noqa: F401
from robert_agent.adapters.notifications.messaging import render_message
from robert_agent.delivery import messages
from robert_agent.delivery.application.notifications import deliver_pending
from robert_agent.execution.notifications import has_pending_local_delivery, update_failure
from robert_agent.persistence import database
from robert_agent.persistence.queries import notifications as notification_queries
from tests.support.work_items import WorkItemCommandFixture


class SourceFailureNotificationTests(WorkItemCommandFixture):
    def failure(self, now, *, key="github:example/repo!21"):
        with closing(database.connect(self.db_path)) as conn, conn:
            update_failure(
                conn,
                "source_reconcile_failed",
                key,
                now,
                {
                    "source_key": key,
                    "repo_id": "repo-1",
                    "workstream_id": key,
                    "reason_code": "source_reconcile_failed",
                    "error_type": "OperationalError",
                    "safe_error": "database is locked",
                },
            )

    def ready(self, now):
        with closing(database.connect(self.db_path)) as conn:
            return has_pending_local_delivery(conn, ("repo-1",), now, command="notify")

    def deliver(self, now, *, limit=100):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        result = deliver_pending(self.db_path, command=["notify"], runner=runner, now=now, limit=limit)
        return result, runner

    def test_transient_failure_is_not_sent_and_recovery_remains_recorded(self):
        self.failure("2026-11-25T02:00:00Z")
        self.assertFalse(self.ready("2026-11-25T02:04:59Z"))
        result, runner = self.deliver("2026-11-25T02:04:59Z")
        self.assertEqual(result["sent"], 0)
        runner.assert_not_called()
        with closing(database.connect(self.db_path)) as conn, conn:
            update_failure(conn, "source_reconcile_failed", "github:example/repo!21", "2026-11-25T02:05:00Z")
        self.assertFalse(self.ready("2026-11-25T02:10:00Z"))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications").fetchone()[0], "resolved")

    def test_sustained_failure_sends_context_and_ack_only(self):
        self._failed_github_task()
        self.failure("2026-11-25T02:00:00Z")
        self.assertTrue(self.ready("2026-11-25T02:05:00Z"))
        result, runner = self.deliver("2026-11-25T02:05:00Z")
        self.assertEqual(result["sent"], 1)
        payload = json.loads(runner.call_args.kwargs["input"])
        self.assertEqual(payload["repository"], "example/repo")
        self.assertEqual(payload["source_key"], "github:example/repo!21")
        self.assertEqual(payload["error_type"], "OperationalError")
        self.assertEqual(payload["safe_error"], "database is locked")
        self.assertEqual(payload["actions"], ["ack"])
        text = render_message(payload)
        self.assertIn("来源状态同步失败", text)
        self.assertIn("https://github.com/example/repo/pull/21", text)
        self.assertIn("/operations", text)
        self.assertNotIn("原因暂未归类", text)
        self.assertNotIn("新通知", text)

    def test_failure_after_recovery_waits_for_hourly_cooldown(self):
        self.failure("2026-11-25T02:00:00Z")
        self.assertEqual(self.deliver("2026-11-25T02:05:00Z")[0]["sent"], 1)
        with closing(database.connect(self.db_path)) as conn, conn:
            update_failure(conn, "source_reconcile_failed", "github:example/repo!21", "2026-11-25T02:06:00Z")
        self.failure("2026-11-25T02:07:00Z")
        self.assertFalse(self.ready("2026-11-25T02:12:00Z"))
        self.assertEqual(self.deliver("2026-11-25T03:04:59Z")[0]["sent"], 0)
        self.assertTrue(self.ready("2026-11-25T03:05:00Z"))
        self.assertEqual(self.deliver("2026-11-25T03:05:00Z")[0]["sent"], 1)

    def test_delayed_source_failure_does_not_starve_another_notice(self):
        self.failure("2026-11-25T02:00:00Z")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json) "
                "VALUES ('other','worker_stale','local','recorded','2026-11-25T02:00:01Z','{}')"
            )
        result, runner = self.deliver("2026-11-25T02:00:02Z", limit=1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(json.loads(runner.call_args.kwargs["input"])["notification_id"], "other")

    def test_failure_from_denied_repository_does_not_leak_context(self):
        self.failure("2026-11-25T02:00:00Z")
        config = {"repos": [{"full_name": "example/repo"}], "messaging": {"repos": []}}
        with closing(database.connect(self.db_path)) as conn:
            row = conn._driver.execute("SELECT notification_id FROM notifications").fetchone()
            self.assertIsNone(messages.envelope(conn, row[0], config=config))

    def test_legacy_taskless_notice_resolves_source_context_and_repository_scope(self):
        self._failed_github_task()
        self.failure("2026-11-25T02:00:00Z")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            row = conn.execute("SELECT notification_id,metadata_json FROM notifications").fetchone()
            metadata = json.loads(row[1])
            metadata.pop("repo_id")
            metadata.pop("workstream_id")
            conn.execute("UPDATE notifications SET metadata_json=?", (json.dumps(metadata),))
        with closing(database.connect(self.db_path)) as conn:
            payload = messages.envelope(conn, row[0])
            self.assertEqual(payload["repository"], "example/repo")
            self.assertEqual(payload["pr_url"], "https://github.com/example/repo/pull/21")
            config = {"repos": [{"full_name": "example/repo"}], "messaging": {"repos": []}}
            self.assertIsNone(messages.envelope(conn, row[0], config=config))

    def test_other_source_is_not_suppressed_by_same_hour_delivery(self):
        self.failure("2026-11-25T02:00:00Z")
        self.assertEqual(self.deliver("2026-11-25T02:05:00Z")[0]["sent"], 1)
        self.failure("2026-11-25T02:06:00Z", key="github:example/repo!22")
        self.assertEqual(self.deliver("2026-11-25T02:11:00Z")[0]["sent"], 1)

    def test_same_source_sent_after_queue_selection_is_rechecked_before_transport(self):
        self.failure("2026-11-25T02:00:00Z")
        original = notification_queries.find_for_deliver_pending

        def select_then_deliver(*args, **kwargs):
            rows = original(*args, **kwargs)
            metadata = {
                "source_key": "github:example/repo!21",
                "delivery": {"status": "sent", "sent_at": "2026-11-25T02:05:00Z"},
            }
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json) "
                    "VALUES ('previous','source_reconcile_failed','local','resolved','2026-11-25T01:50:00Z',?)",
                    (json.dumps(metadata),),
                )
            return rows

        with patch.object(notification_queries, "find_for_deliver_pending", side_effect=select_then_deliver):
            result, runner = self.deliver("2026-11-25T02:05:00Z")
        self.assertEqual(result["sent"], 0)
        runner.assert_not_called()

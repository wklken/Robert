"""Sparse notifications must explain their business context without gaining authority."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest.mock import Mock, patch

from robert_agent.adapters.notifications.messaging import render_message
from robert_agent.delivery import messages, notifications as publication_notifications
from robert_agent.delivery.application import notifications
from robert_agent.persistence import database
from robert_agent.persistence.stores import notifications as notification_store
from tests.support.work_items import WorkItemCommandFixture


class MessageContextTests(WorkItemCommandFixture):
    def test_sparse_global_decision_alert_explains_service_inspection_and_ack(self):
        self.notice(
            "service-alert",
            "2026-11-12T06:55:01Z",
            kind="supervision_decision_unavailable",
            metadata={
                "reason_code": "exited",
                "required_action": "Inspect the supervisor decision service; automatic decisions are waiting.",
            },
        )
        with closing(database.connect(self.db_path)) as conn:
            payload = messages.envelope(conn, "service-alert", config=self.config)
        text = render_message({**payload, "delivery_id": "service-alert:receipt"})
        self.assertIsNone(payload["task_id"])
        self.assertEqual(payload["actions"], ["ack"])
        self.assertIn("全局", text)
        self.assertIn("/operations", text)
        self.assertIn("robert supervisor status", text)
        self.assertIn("非零", text)
        self.assertIn("仅记录收悉", text)
        self.assertNotIn("原因暂未归类", text)
        self.assertNotIn("请查看任务详情", text)
        self.assertNotIn("Inspect the supervisor", text)

        with closing(database.connect(self.db_path)) as conn, conn:
            notification_store.update_for_persist_control_failure_alerts(conn, "service-alert")
        with closing(database.connect(self.db_path)) as conn:
            recovered = messages.envelope(conn, "service-alert", config=self.config, include_closed=True)
        self.assertIn("已恢复", recovered["summary"])
        self.assertNotIn("检查决策服务", recovered["required_action"])

    def test_command_transport_and_readiness_honor_explicit_cutoff(self):
        self.notice("history", "2026-07-17T09:58:43+00:00")
        config = {"messaging": {"enabled": False, "send_after": "2026-11-09T00:00:00Z"}}
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with patch.dict("os.environ", {"ROBERT_NOTIFICATION_COMMAND": "local-notifier"}, clear=True):
            self.assertEqual(notifications.deliver_pending(self.db_path, config=config, runner=runner)["sent"], 0)
            from robert_agent.execution.notifications import has_pending_local_delivery

            with closing(database.connect(self.db_path)) as conn:
                self.assertFalse(has_pending_local_delivery(conn, (), "2026-11-09T00:01:00Z", config=config))
            self.notice("future", "2026-11-09T00:00:01Z")
            self.assertEqual(notifications.deliver_pending(self.db_path, config=config, runner=runner)["sent"], 1)
        self.assertEqual(runner.call_count, 1)

    def test_publication_retry_preserves_initial_notification_time(self):
        task_id = self._failed_github_task()
        action = {"task_id": task_id, "action_id": "action-cutoff", "action_type": "comment"}
        original_time = "2026-11-08T23:59:59Z"
        result = {"reason_code": "retry_scheduled", "safe_error": "network unavailable"}
        with closing(database.connect(self.db_path)) as conn, conn:
            publication_notifications.insert_notification(conn, action, "github_publish_failed", result, original_time)
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True):
            for resolve_first in (False, True):
                with self.subTest(resolve_first=resolve_first):
                    with closing(database.connect(self.db_path)) as conn, conn:
                        if resolve_first:
                            publication_notifications.resolve_action_notification(
                                conn, action, "github_publish_failed", {"status": "published"}, "2026-11-09T00:01:00Z"
                            )
                        publication_notifications.insert_notification(
                            conn, action, "github_publish_failed", result, "2026-11-09T00:02:00Z"
                        )
                    self.assertEqual(
                        notifications.deliver_pending(self.db_path, config=self.config, runner=runner)["sent"], 0
                    )
                    with closing(sqlite3.connect(self.db_path)) as conn:
                        self.assertEqual(
                            conn.execute("SELECT created_at FROM notifications").fetchall(), [(original_time,)]
                        )
        runner.assert_not_called()

    def setUp(self):
        super().setUp()
        self.config = {
            "repos": [{"full_name": "example/repo"}],
            "messaging": {
                "enabled": True,
                "provider": "openclaw",
                "channel": "wecom",
                "account": "default",
                "target": "wecom:operator",
                "repos": ["example/repo"],
                "send_after": "2026-11-09T00:00:00+00:00",
            },
        }

    def notice(self, identifier, created, *, task_id=None, kind="worker_stale", metadata=None):
        with closing(database.connect(self.db_path)) as conn, conn:
            notification_store.record_notification(
                conn,
                identifier,
                task_id,
                kind,
                "local",
                "recorded",
                created,
                json.dumps(metadata or {}),
            )

    def test_future_failure_retries_after_restart_and_never_replays_history(self):
        self.notice("history", "2026-07-17T09:58:43+00:00")
        self.notice("future", "2026-11-09T00:00:01+00:00")
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess([], 1, "", "unavailable"),
                subprocess.CompletedProcess([], 0, '{"ok":true}', ""),
            ]
        )
        with patch.dict("os.environ", {}, clear=True):
            first = notifications.deliver_pending(
                self.db_path,
                config=self.config,
                runner=runner,
                now="2026-11-09T00:01:00Z",
                limit=1,
            )
            second = notifications.deliver_pending(
                self.db_path,
                config=json.loads(json.dumps(self.config)),
                runner=runner,
                now="2026-11-09T00:02:00Z",
                limit=1,
            )
            third = notifications.deliver_pending(
                self.db_path,
                config=self.config,
                runner=runner,
                now="2026-11-09T00:03:00Z",
                limit=1,
            )
        self.assertEqual((first["failed"], second["sent"], third["sent"]), (1, 1, 0))
        texts = [call.args[0][call.args[0].index("--message") + 1] for call in runner.call_args_list]
        self.assertEqual(texts[0], texts[1])
        self.assertIn("future", texts[0])
        self.assertNotIn("history", texts[0])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status,metadata_json FROM notifications WHERE notification_id='history'"
                ).fetchone(),
                ("recorded", "{}"),
            )

    def test_sparse_worker_notice_reads_title_link_state_time_and_attempt_reason(self):
        task_id = self._failed_github_task()
        failure = {
            "status": "needs_resume",
            "original_status": "failed_timeout",
            "recovery": {"status": "recoverable_progress"},
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,failure_json) VALUES (?,?,1,'failed',?)",
                ("attempt-context", task_id, json.dumps(failure)),
            )
        self.notice(
            "context",
            "2026-11-09T00:00:01+00:00",
            task_id=task_id,
            kind="worker_resume_prepared",
            metadata={"attempt_id": "attempt-context"},
        )
        with closing(database.connect(self.db_path)) as conn:
            payload = messages.envelope(conn, "context", config=self.config)
        text = render_message({**payload, "delivery_id": "context:receipt"})
        for value in (
            "example/repo",
            "Update AIDev integration",
            "https://github.com/example/repo/pull/21",
            "执行失败",
            "2026-11-09T00:00:01+00:00",
            "任务执行超过允许的时长",
            "发现已保存的执行进展",
        ):
            self.assertIn(value, text)
        self.assertEqual(payload["reason_code"], "failed_timeout")
        self.assertEqual(payload["recovery_status"], "recoverable_progress")
        self.assertEqual(payload["actions"], ["ack"])
        self.assertIn("确认收到", text)
        self.assertIn("/robert-task " + task_id, text)

    def test_question_context_keeps_the_existing_version_and_actions(self):
        item = self._create(start=True)["item"]
        event = self._question(item["work_item_id"], 1)
        with closing(database.connect(self.db_path)) as conn:
            payload = messages.envelope(conn, "attention-" + event["event_id"], config=self.config)
        self.assertEqual(payload["actions"], ["reply", "cancel"])
        self.assertEqual(payload["version"], item["version"])
        self.assertEqual(payload["repository"], "example/repo")
        self.assertEqual(payload["task_status"], "waiting_for_user")
        self.assertEqual(payload["summary"], "Question 1?")

    def test_cleanup_notice_explains_unverified_stop(self):
        self.notice("cleanup", "2026-11-09T00:00:01Z", kind="worker_cleanup_pending")
        with closing(database.connect(self.db_path)) as conn:
            payload = messages.envelope(conn, "cleanup")
        body = render_message({**payload, "delivery_id": "cleanup:receipt"})
        self.assertIn("无法确认执行进程已停止", body)
        self.assertIn("检查执行进程", payload["summary"])
        self.assertNotIn("worker_cleanup_pending", body)

    def test_sensitive_context_withholds_details_and_mutating_actions(self):
        item = self._create(start=True)["item"]
        event = self._question(item["work_item_id"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE work_items SET title='password=supersecret'")
        with closing(database.connect(self.db_path)) as conn:
            payload = messages.envelope(conn, "attention-" + event["event_id"], config=self.config)
        text = render_message({**payload, "delivery_id": "safe"})
        self.assertNotIn("supersecret", text)
        self.assertEqual(payload["actions"], ["ack"])
        self.assertIn("Details require local inspection.", text)

    def test_disabled_or_missing_cutoff_cannot_run_transport(self):
        self.notice("future", "2026-11-09T00:00:01+00:00")
        runner = Mock(side_effect=AssertionError("No external sends are permitted"))
        with patch.dict("os.environ", {}, clear=True):
            disabled = {**self.config, "messaging": {**self.config["messaging"], "enabled": False}}
            self.assertEqual(
                notifications.deliver_pending(self.db_path, config=disabled, runner=runner)["status"], "not_configured"
            )
            incomplete = json.loads(json.dumps(self.config))
            del incomplete["messaging"]["send_after"]
            self.assertEqual(notifications.deliver_pending(self.db_path, config=incomplete, runner=runner)["sent"], 0)
        runner.assert_not_called()

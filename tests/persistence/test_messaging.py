"""Messaging queries and writes preserve caller-owned transactions and queue scope."""

from contextlib import closing
import json
import sqlite3

from robert_agent.persistence import database, transactions
from robert_agent.persistence.queries import notifications
from robert_agent.persistence.stores import notifications as notification_store
from robert_agent.work.application import commands
from tests.support.work_items import WorkItemCommandFixture


class MessagingPersistenceTests(WorkItemCommandFixture):
    def test_delivery_cutoff_excludes_backlog_before_limit_without_hiding_inbox(self):
        settings = {"enabled": True, "send_after": "2026-11-07T12:00:00Z"}
        now = "2026-11-07T12:02:00Z"
        with closing(database.connect(self.db_path)) as conn, conn:
            for index in range(105):
                notification_store.record_notification(
                    conn,
                    f"history-{index}",
                    None,
                    "worker_stale",
                    "local",
                    "recorded",
                    "2026-07-17T09:58:43Z",
                    "{}",
                )
            for identifier, created in (("boundary", "2026-11-07T20:00:00+08:00"), ("invalid", "invalid")):
                notification_store.record_notification(
                    conn,
                    identifier,
                    None,
                    "worker_stale",
                    "local",
                    "recorded",
                    created,
                    "{}",
                )
            self.assertFalse(
                notifications.has_pending_local_delivery_filtered(conn, (), now, messaging_settings=settings)
            )
            self.assertEqual(notifications.find_for_deliver_pending(conn, now, 1, messaging_settings=settings), [])
            self.assertTrue(notifications.find_inbox(conn, messaging_repo_ids=None))
            notification_store.record_notification(
                conn,
                "future",
                None,
                "worker_stale",
                "local",
                "recorded",
                "2026-11-07T12:01:00Z",
                "{}",
            )
            self.assertEqual(
                [row[0] for row in notifications.find_for_deliver_pending(conn, now, 1, messaging_settings=settings)],
                ["future"],
            )
            self.assertTrue(
                notifications.has_pending_local_delivery_filtered(conn, (), now, messaging_settings=settings)
            )
            self.assertFalse(notifications.find_for_deliver_pending(conn, now, 1, messaging_settings={"enabled": True}))
            self.assertEqual(notifications.find_metadata_json_by_notification_id(conn, "history-0")[0][0], "{}")

    def test_event_and_attention_roll_back_together_on_orm_connection(self):
        item = self._create()["item"]
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin_immediate(conn)
            event = commands.record_system_event(
                conn,
                item["work_item_id"],
                event_type="operator_question",
                idempotency_key="uncommitted-question",
                body="Proceed?",
                now="2026-11-07T12:00:00Z",
            )
            self.assertTrue(notifications.find_local_response(conn, "attention-" + event["event_id"]))
            with closing(sqlite3.connect(self.db_path)) as observer:
                self.assertEqual(observer.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)
            conn.rollback()
        with closing(sqlite3.connect(self.db_path)) as observer:
            self.assertEqual(observer.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)
            self.assertEqual(
                observer.execute(
                    "SELECT count(*) FROM work_item_events WHERE event_type='operator_question'"
                ).fetchone()[0],
                0,
            )

    def test_correlated_notice_preserves_receipts_and_resolution_can_roll_back(self):
        notice_id = "notice-'quoted"
        metadata = {"delivery": {"status": "sent"}, "reason_code": "waiting"}
        with closing(database.connect(self.db_path)) as conn, conn:
            notification_store.record_notification(
                conn,
                notice_id,
                None,
                "execution_attention_required",
                "local",
                "recorded",
                "2026-11-07T12:00:00Z",
                json.dumps(metadata),
            )
            notification_store.record_attention(
                conn,
                notification_id=notice_id,
                task_id=None,
                event_type="operator_question",
                now="2026-11-07T12:01:00Z",
                metadata_json=json.dumps({"attention_event_id": "event-'quoted", "work_item_id": "item-1"}),
            )
        with closing(database.connect(self.db_path)) as conn:
            transactions.begin_immediate(conn)
            notification_store.resolve_attention(conn, "event-'quoted")
            row = notifications.find_task_id_and_notification_type_and_status_and_metadata_json_by_notification_id(
                conn, notice_id
            )[0]
            self.assertEqual(row[2], "resolved")
            self.assertEqual(json.loads(row[3])["delivery"], metadata["delivery"])
            conn.rollback()
        with closing(database.connect(self.db_path)) as conn:
            row = notifications.find_task_id_and_notification_type_and_status_and_metadata_json_by_notification_id(
                conn, notice_id
            )[0]
            self.assertEqual(row[2], "recorded")
            self.assertEqual(row[1], "execution_attention_required")
            self.assertEqual(json.loads(row[3])["attention_event_id"], "event-'quoted")

    def test_queue_scope_filters_before_limits_for_inbox_delivery_and_readiness(self):
        now = "2026-11-07T12:00:00Z"
        with closing(database.connect(self.db_path)) as conn, conn:
            for index in range(105):
                notification_store.record_notification(
                    conn,
                    f"excluded-{index}",
                    None,
                    "supervision_incident",
                    "local",
                    "recorded",
                    "2026-11-06T12:00:00Z" if index % 2 else "2026-11-08T12:00:00Z",
                    json.dumps({"repo_id": "repo-other"}),
                )
            self.assertFalse(
                notifications.has_pending_local_delivery_filtered(conn, (), now, messaging_repo_ids={"repo-1"})
            )
            notification_store.record_notification(
                conn,
                "allowed",
                None,
                "supervision_incident",
                "local",
                "recorded",
                now,
                json.dumps({"repo_id": "repo-1"}),
            )
            self.assertEqual(
                [row[0] for row in notifications.find_inbox(conn, messaging_repo_ids={"repo-1"}, limit=1)], ["allowed"]
            )
            self.assertEqual(
                [row[0] for row in notifications.find_for_deliver_pending(conn, now, 1, messaging_repo_ids={"repo-1"})],
                ["allowed"],
            )
            self.assertTrue(
                notifications.has_pending_local_delivery_filtered(conn, (), now, messaging_repo_ids={"repo-1"})
            )
            self.assertFalse(notifications.find_inbox(conn, messaging_repo_ids=set()))
            self.assertFalse(notifications.find_for_deliver_pending(conn, now, 1, messaging_repo_ids=set()))
            self.assertTrue(notifications.find_for_deliver_pending(conn, now, 1, messaging_repo_ids=None))

    def test_resolved_question_is_excluded_even_if_notification_state_is_stale(self):
        item = self._create(start=True)["item"]
        self._question(item["work_item_id"], 1)
        commands.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=item["version"],
            idempotency_key="answer",
            body="Proceed.",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE notifications SET status='recorded'")
        with closing(database.connect(self.db_path)) as conn:
            self.assertFalse(notifications.find_inbox(conn, messaging_repo_ids={"repo-1"}))
            self.assertFalse(notifications.find_for_deliver_pending(conn, "2026-11-07T12:00:00Z", 1))
            self.assertFalse(
                notifications.has_pending_local_delivery_filtered(conn, ["repo-1"], "2026-11-07T12:00:00Z")
            )

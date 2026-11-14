"""Dedicated chat replies select delivered questions and retain command authority."""

from contextlib import closing
import json
import sqlite3
import subprocess
import sys
from unittest.mock import Mock, patch

from robert_agent.persistence import database, transactions
from robert_agent.supervision import decisions
from robert_agent.delivery.application import notifications
from robert_agent.work.application import commands, messaging
from robert_agent.work.errors import WorkItemConflictError, WorkItemValidationError
from tests.support.work_items import WorkItemCommandFixture


class ContextualMessagingTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self.identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "wecom",
            "account": "default",
            "conversation": "wecom:operator",
            "thread": "",
            "sender_id": "owner",
        }
        self.config = {
            "db_path": str(self.db_path),
            "workers": [{"name": "default"}],
            "repos": [{"full_name": "example/repo"}],
            "messaging": {
                **{k: v for k, v in self.identity.items() if k not in {"protocol", "sender_id"}},
                "enabled": True,
                "contextual_replies": True,
                "target": "operator",
                "send_after": "2026-07-18T00:00:00+00:00",
                "allowed_senders": ["owner"],
                "repos": ["example/repo"],
            },
        }
        self.first = self._create(start=True)["item"]
        self.question = self._question(self.first["work_item_id"], 1)
        self.notice = "attention-" + self.question["event_id"]
        self.request = {**self.identity, "message_id": "inbound-1", "body": "Use release/1.25.\nKeep other defaults."}

    def deliver(self, now="2026-07-19T02:00:00+00:00", remote_id="outbound-1"):
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 0, json.dumps({"payload": {"messageId": remote_id}}), "")
        )
        with patch.dict("os.environ", {}, clear=True):
            return notifications.deliver_pending(self.db_path, config=self.config, runner=runner, now=now)

    def second_question(self):
        item = self._create(start=True, idempotency_key="create-2", title="Second task")["item"]
        question = self._question(item["work_item_id"], 2)
        return item, "attention-" + question["event_id"]

    def supervisor_question(self):
        item = self._create(start=True, idempotency_key="supervisor-goal", title="Supervised task")["item"]
        self.config["supervision"] = {"mode": "decide", "decision": {"command": [sys.executable]}}
        now = "2026-07-19T02:30:00+00:00"
        with closing(database.connect(self.db_path)) as conn, conn:
            transactions.begin_immediate(conn)
            packet = decisions.packet(decisions.snapshot(conn, item["workstream_id"], self.config, now), {})
            output = {
                "bundle_id": packet["bundle_id"],
                "bundle_sha256": packet["bundle_sha256"],
                "action": "ask_user",
                "candidate_id": None,
                "reason": "The supervised goal needs clarification",
                "evidence_ids": ["goals"],
                "expected_progress": "Receive an answer before continuing supervision",
                "recheck_seconds": 300,
                "message": "Which behavior should the supervised task preserve?",
            }
            self.assertTrue(
                decisions.approve(conn, self.config, packet, decisions.validate(json.dumps(output), packet), now)
            )
        envelope = next(
            entry
            for entry in messaging.inbox(self.config, self.identity)["messages"]
            if entry["notification_id"] == packet["bundle_id"]
        )
        self.assertEqual(envelope["type"], "supervision_question")
        self.assertEqual(envelope["actions"], ["reply", "cancel"])
        return item, envelope

    def assert_supervisor_answer(self, item, notice, result):
        self.assertEqual(result["notification_id"], notice["notification_id"])
        self.assertIsNone(result["task_id"])
        with closing(database.connect(self.db_path)) as conn:
            state = decisions.read(conn, item["workstream_id"])
            self.assertEqual(state["status"], "reply_received")
            self.assertEqual(state["reply"], self.request["body"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(
                conn.execute(
                    "SELECT resolves_event_id FROM work_item_events WHERE event_type='user_response'"
                ).fetchone()[0],
                notice["event_id"],
            )
        self.assertEqual(messaging.inbox(self.config, self.identity)["messages"][0]["notification_id"], self.notice)

    def test_redacted_reply_replay_preserves_receipt_without_storing_sensitive_body(self):
        self.deliver()
        request = {**self.request, "body": "Use /Users/example/work/project and the service at 10.0.0.12."}
        result = messaging.reply(self.config, request)
        self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        self.assertEqual(messaging.reply(self.config, request), result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            responses = conn.execute("SELECT body FROM work_item_events WHERE event_type='user_response'").fetchall()
            self.assertEqual(responses, [("Use <local-path> and the service at <internal-ip>.",)])
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 3)

    def test_latest_delivered_supervisor_question_receives_reply_without_starting_older_task(self):
        self.deliver()
        item, notice = self.supervisor_question()
        self.assertEqual(self.deliver("2026-07-19T03:00:00+00:00", "supervisor-outbound")["sent"], 1)
        result = messaging.reply(self.config, self.request)
        self.assert_supervisor_answer(item, notice, result)
        self.assertEqual(messaging.reply(self.config, self.request), result)

    def test_supervisor_question_can_be_quoted_by_transport_id(self):
        self.deliver()
        item, notice = self.supervisor_question()
        self.deliver("2026-07-19T03:00:00+00:00", "supervisor-outbound")
        result = messaging.reply(
            self.config, {**self.request, "quoted": True, "quote_message_id": "supervisor-outbound"}
        )
        self.assert_supervisor_answer(item, notice, result)

    def test_supervisor_question_can_be_quoted_by_command_text(self):
        self.deliver()
        item, notice = self.supervisor_question()
        self.deliver("2026-07-19T03:00:00+00:00", "supervisor-outbound")
        result = messaging.reply(
            self.config,
            {
                **self.request,
                "quote_body": f"/robert-respond {notice['notification_id']} {notice['version']} reply <answer>",
            },
        )
        self.assert_supervisor_answer(item, notice, result)

    def test_undelivered_supervisor_question_does_not_displace_delivered_question(self):
        self.deliver()
        self.supervisor_question()
        self.assertEqual(messaging.reply(self.config, self.request)["notification_id"], self.notice)

    def test_supervisor_quote_retains_repository_endpoint_and_version_checks(self):
        self.deliver()
        item, notice = self.supervisor_question()
        self.deliver("2026-07-19T03:00:00+00:00", "supervisor-outbound")
        request = {**self.request, "quoted": True, "quote_message_id": "supervisor-outbound"}
        denied = {**self.config, "messaging": {**self.config["messaging"], "repos": []}}
        moved = {**self.config, "messaging": {**self.config["messaging"], "conversation": "wecom:new"}}
        for config, inbound in ((denied, request), (moved, {**request, "conversation": "wecom:new"})):
            with self.subTest(settings=config["messaging"]), self.assertRaises(WorkItemValidationError):
                messaging.reply(config, inbound)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE work_items SET version=version+1 WHERE work_item_id=?", (item["work_item_id"],))
        with self.assertRaises(WorkItemConflictError):
            messaging.reply(self.config, request)
        with closing(database.connect(self.db_path)) as conn:
            self.assertEqual(decisions.read(conn, item["workstream_id"])["status"], "ask_user")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM work_item_events WHERE event_type='user_response'").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_latest_skips_attention_that_only_authorizes_cancel(self):
        self.deliver()
        item = self._create(start=True, idempotency_key="cutover-task")["item"]
        with closing(database.connect(self.db_path)) as conn, conn:
            commands.record_system_event(
                conn,
                item["work_item_id"],
                event_type="operator_question",
                idempotency_key="cutover-question",
                body="This task must be canceled locally.",
                metadata={"kind": "result_contract_cutover"},
                now="2026-07-19T02:30:00+00:00",
            )
        self.deliver("2026-07-19T03:00:00+00:00", "cutover-outbound")
        self.assertEqual(messaging.reply(self.config, self.request)["notification_id"], self.notice)

    def test_latest_delivered_question_and_replay_survive_newer_question(self):
        self.deliver()
        result = messaging.reply(self.config, self.request)
        self.assertEqual(result["notification_id"], self.notice)
        second, _ = self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        self.assertEqual(messaging.reply(self.config, self.request), result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 3)
            self.assertEqual(
                conn.execute("SELECT body FROM work_item_events WHERE event_type='user_response'").fetchone()[0],
                self.request["body"],
            )
        self.assertEqual(
            messaging.inbox(self.config, self.identity)["messages"][0]["work_item_id"], second["work_item_id"]
        )

    def test_quote_wins_over_newer_question_by_text_or_transport_id(self):
        self.deliver()
        self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        result = messaging.reply(
            self.config,
            {
                **self.request,
                "quoted": True,
                "quote_body": f"/robert-respond {self.notice} 1 reply <answer>",
                "quote_message_id": "outbound-1",
            },
        )
        self.assertEqual(result["notification_id"], self.notice)

    def test_transport_quote_without_body_is_supported(self):
        self.deliver()
        self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        result = messaging.reply(self.config, {**self.request, "quoted": True, "quote_message_id": "outbound-1"})
        self.assertEqual(result["notification_id"], self.notice)

    def test_latest_skips_undelivered_questions_and_unrelated_notices(self):
        self.deliver()
        self.second_question()
        notifications.record_notification(self.db_path, "supervision_incident", "recorded", {"summary": "FYI"})
        result = messaging.reply(self.config, self.request)
        self.assertEqual(result["notification_id"], self.notice)

    def test_questions_delivered_in_one_batch_select_newer_question(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE notifications SET notification_id='z-older-question' WHERE notification_id=?", (self.notice,)
            )
        _, second_notice = self.second_question()
        self.deliver()
        self.assertEqual(messaging.reply(self.config, self.request)["notification_id"], second_notice)

    def test_unknown_closed_and_ambiguous_quotes_never_fall_back(self):
        self.deliver()
        self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        for body in (
            "An unrelated message",
            "Delivery: unknown:hash",
            f"/robert-respond {self.notice} 1 reply\n/robert-respond another 1 reply",
        ):
            with self.subTest(body=body), self.assertRaises(WorkItemValidationError):
                messaging.reply(self.config, {**self.request, "quoted": True, "quote_body": body})
        commands.execute_command(
            self.db_path,
            self.first["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=self.first["version"],
            expected_event_id=self.question["event_id"],
            idempotency_key="local-answer",
            body="Handled locally.",
        )
        with self.assertRaises(WorkItemConflictError):
            messaging.reply(self.config, {**self.request, "quoted": True, "quote_message_id": "outbound-1"})

    def test_delivered_version_is_not_silently_replaced(self):
        self.deliver()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE work_items SET version=version+1")
        with self.assertRaises(WorkItemConflictError):
            messaging.reply(self.config, self.request)

    def test_no_pending_disabled_unauthorized_and_sensitive_input_do_not_mutate(self):
        self.assertEqual(messaging.reply(self.config, self.request)["status"], "no_question")
        self.deliver()
        for field, value in (
            ("conversation", "other"),
            ("sender_id", "other"),
            ("thread", "7"),
            ("channel", "webchat"),
        ):
            with self.subTest(field=field):
                self.assertEqual(messaging.reply(self.config, {**self.request, field: value})["status"], "ignored")
        disabled = {**self.config, "messaging": {**self.config["messaging"], "contextual_replies": False}}
        self.assertEqual(messaging.reply(disabled, self.request)["status"], "ignored")
        for request in ({**self.request, "body": "password=supersecret"}, {**self.request, "message_id": ""}):
            with self.assertRaises(WorkItemValidationError):
                messaging.reply(self.config, request)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_same_inbound_id_with_changed_body_is_rejected(self):
        self.deliver()
        messaging.reply(self.config, self.request)
        self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        with self.assertRaises(WorkItemConflictError):
            messaging.reply(self.config, {**self.request, "body": "Different reply"})

    def test_quote_text_alone_selects_older_question(self):
        self.deliver()
        self.second_question()
        self.deliver("2026-07-19T03:00:00+00:00", "outbound-2")
        result = messaging.reply(
            self.config,
            {**self.request, "quote_body": f"/robert-respond {self.notice} {self.first['version']} reply <answer>"},
        )
        self.assertEqual(result["notification_id"], self.notice)

    def test_concurrent_replay_is_refused_without_answering_another_question(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        self.deliver()
        entered, release = Event(), Event()
        execute = commands.execute_command

        def held_command(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Concurrent reply did not complete")
            return execute(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(commands, "execute_command", held_command):
            pending = executor.submit(messaging.reply, self.config, self.request)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(WorkItemConflictError):
                    messaging.reply(self.config, self.request)
            finally:
                release.set()
            result = pending.result(timeout=5)
            self.assertEqual(result["notification_id"], self.notice)
        self.assertEqual(messaging.reply(self.config, self.request)["notification_id"], self.notice)

    def test_denied_repository_and_changed_endpoint_do_not_select_question(self):
        self.deliver()
        denied = {**self.config, "messaging": {**self.config["messaging"], "repos": []}}
        self.assertEqual(messaging.reply(denied, self.request)["status"], "no_question")
        moved = {**self.config, "messaging": {**self.config["messaging"], "conversation": "wecom:new"}}
        self.assertEqual(messaging.reply(moved, {**self.request, "conversation": "wecom:new"})["status"], "no_question")

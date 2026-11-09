"""Operator chat replies reuse durable task authority and command receipts."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest.mock import Mock, patch

from robert_agent.delivery.application import notifications
from robert_agent.work.application import messaging, commands
from robert_agent.work.errors import WorkItemConflictError, WorkItemValidationError, WorkItemNotFoundError
from tests.support.work_items import WorkItemCommandFixture


class OperatorMessagingTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self.config = {
            "db_path": str(self.db_path),
            "workers": [{"name": "default"}],
            "repos": [{"full_name": "example/repo"}],
            "messaging": {
                "enabled": True,
                "send_after": "2026-07-18T00:00:00+00:00",
                "provider": "openclaw",
                "channel": "telegram",
                "account": "personal",
                "conversation": "telegram:42",
                "target": "42",
                "thread": "",
                "allowed_senders": ["42"],
                "repos": ["example/repo"],
            },
        }
        self.identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "account": "personal",
            "conversation": "telegram:42",
            "thread": "",
            "sender_id": "42",
        }
        self.item = self._create(start=True)["item"]
        self.question = self._question(self.item["work_item_id"], 1)
        self.notice_id = "attention-" + self.question["event_id"]
        self.reply = {
            **self.identity,
            "notification_id": self.notice_id,
            "version": self.item["version"],
            "action": "reply",
            "body": "Use SQLite.",
        }

    def test_question_delivery_reply_and_duplicate_resume_once(self):
        # A real SQLite writer in the fake transport proves no writer is held
        # during external effects.
        def send(argv, **kwargs):
            with closing(sqlite3.connect(self.db_path, timeout=0)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
            self.assertEqual(argv[:3], ["openclaw", "message", "send"])
            text = argv[argv.index("--message") + 1]
            self.assertIn("Question 1?", text)
            self.assertIn(f"/robert-respond {self.notice_id} {self.item['version']} reply", text)
            return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")

        runner = Mock(side_effect=send)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(notifications.deliver_pending(self.db_path, config=self.config, runner=runner)["sent"], 1)
            notifications.deliver_pending(self.db_path, config=self.config, runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(len(messaging.inbox(self.config, self.identity)["messages"]), 1)
        result = messaging.respond(self.config, self.reply)
        self.assertEqual(messaging.respond(self.config, self.reply), result)
        self.assertEqual(messaging.inbox(self.config, self.identity)["messages"], [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            response = conn.execute(
                "SELECT actor_identity,body FROM work_item_events WHERE event_type='user_response'"
            ).fetchone()
            self.assertEqual(response, ("openclaw:telegram:personal:42", "Use SQLite."))
            child = conn.execute("SELECT parent_task_id FROM tasks WHERE task_id=?", (result["task_id"],)).fetchone()
            self.assertIsNotNone(child[0])

    def test_identity_and_repo_scope_are_fail_closed(self):
        for key, value in [
            ("provider", "hermes"),
            ("channel", "slack"),
            ("account", "other"),
            ("conversation", "telegram:99"),
            ("thread", "99"),
            ("sender_id", "99"),
        ]:
            with self.subTest(key=key), self.assertRaises(WorkItemValidationError):
                messaging.respond(self.config, {**self.reply, key: value})
        config = {**self.config, "messaging": {**self.config["messaging"], "repos": []}}
        self.assertEqual(messaging.inbox(config, self.identity)["messages"], [])
        with self.assertRaises(WorkItemNotFoundError):
            messaging.respond(config, self.reply)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_old_answer_cannot_answer_next_question(self):
        result = messaging.respond(self.config, self.reply)
        self._question(self.item["work_item_id"], 2)
        with self.assertRaises(WorkItemConflictError):
            messaging.respond(self.config, {**self.reply, "version": result["version"], "body": "Different answer"})
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_stale_version_and_running_attempt_are_rejected(self):
        with self.assertRaises(WorkItemConflictError):
            messaging.respond(self.config, {**self.reply, "version": self.item["version"] + 1})
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status='running',finished_at=NULL")
        with self.assertRaises(WorkItemConflictError):
            messaging.respond(self.config, self.reply)

    def test_redaction_and_secret_reply_rejection(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE work_item_events SET body=? WHERE event_id=?",
                ("token=" + "ghp_" + "a" * 25, self.question["event_id"]),
            )
        message = messaging.inbox(self.config, self.identity)["messages"][0]
        self.assertEqual(message["actions"], ["ack"])
        self.assertNotIn("ghp_", json.dumps(message))
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, self.reply)
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, {**self.reply, "body": "password=supersecret"})

    def test_failure_retry_creates_one_child(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("UPDATE tasks SET lifecycle='failed'")
            commands.record_system_event(
                conn,
                self.item["work_item_id"],
                event_type="execution_failed",
                idempotency_key="failure",
                body="Execution failed",
                now="2026-11-07T10:00:00Z",
                metadata={"task_id": self.question["metadata"]["task_id"]},
            )
            event_id = conn.execute(
                "SELECT event_id FROM work_item_events WHERE event_type='execution_failed'"
            ).fetchone()[0]
        reply = {**self.reply, "notification_id": "attention-" + event_id, "action": "retry", "body": ""}
        result = messaging.respond(self.config, reply)
        self.assertEqual(messaging.respond(self.config, reply), result)
        self.assertIsNotNone(result["task_id"])

    def test_supervisor_ack_is_a_receipt_not_recovery(self):
        notifications.record_notification(self.db_path, "supervision_incident", "recorded", {"reason_code": "stalled"})
        with closing(sqlite3.connect(self.db_path)) as conn:
            identifier = conn.execute(
                "SELECT notification_id FROM notifications WHERE notification_type='supervision_incident'"
            ).fetchone()[0]
        request = {**self.reply, "notification_id": identifier, "version": 0, "action": "ack", "body": "Investigating."}
        result = messaging.respond(self.config, request)
        self.assertEqual(messaging.respond(self.config, request), result)
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, {**request, "action": "retry"})
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            status, raw = conn.execute(
                "SELECT status,metadata_json FROM notifications WHERE notification_id=?", (identifier,)
            ).fetchone()
            self.assertEqual(status, "recorded")
            self.assertEqual(json.loads(raw)["operator_response"]["body"], "Investigating.")

    def test_delivery_failure_retries_with_same_delivery_id(self):
        runner = Mock(
            side_effect=[
                subprocess.TimeoutExpired("openclaw", 30),
                subprocess.CompletedProcess([], 0, '{"ok":true}', ""),
            ]
        )
        with patch.dict("os.environ", {}, clear=True):
            first = notifications.deliver_pending(
                self.db_path, config=self.config, runner=runner, now="2026-11-07T00:00:00Z"
            )
            second = notifications.deliver_pending(
                self.db_path, config=self.config, runner=runner, now="2026-11-07T00:01:00Z"
            )
        self.assertEqual((first["failed"], second["sent"]), (1, 1))
        texts = [call.args[0][call.args[0].index("--message") + 1] for call in runner.call_args_list]
        self.assertEqual(texts[0], texts[1])

    def test_closed_attention_cannot_starve_later_notice(self):
        commands.execute_command(
            self.db_path,
            self.item["work_item_id"],
            context=self.context,
            command="cancel",
            expected_version=self.item["version"],
            idempotency_key="cancel",
        )
        notifications.record_notification(self.db_path, "supervision_incident", "recorded", {"reason_code": "stalled"})
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True):
            result = notifications.deliver_pending(self.db_path, config=self.config, runner=runner, limit=1)
        self.assertEqual(result["sent"], 1)
        argv = runner.call_args.args[0]
        self.assertIn("监督服务发现异常", argv[argv.index("--message") + 1])
        inbox = messaging.inbox(self.config, self.identity)["messages"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["type"], "supervision_incident")

    def test_ack_cannot_hide_an_unanswered_question(self):
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, {**self.reply, "action": "ack"})
        self.assertEqual(len(messaging.inbox(self.config, self.identity)["messages"]), 1)

    def test_secret_choice_blocks_reply_and_ack_keeps_question_visible(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            metadata = {**self.question["metadata"], "choices": [{"id": "one", "label": "password=supersecret"}]}
            conn.execute(
                "UPDATE work_item_events SET metadata_json=? WHERE event_id=?",
                (json.dumps(metadata), self.question["event_id"]),
            )
        self.assertEqual(messaging.inbox(self.config, self.identity)["messages"][0]["actions"], ["ack"])
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(self.config, self.reply)
        messaging.respond(self.config, {**self.reply, "action": "ack", "body": "Inspecting locally."})
        self.assertEqual(len(messaging.inbox(self.config, self.identity)["messages"]), 1)

    def test_missing_database_is_not_created_by_chat_ingress(self):
        missing = self.db_path.with_name("absent.sqlite3")
        config = {**self.config, "db_path": str(missing)}
        with self.assertRaises(WorkItemValidationError):
            messaging.inbox(config, self.identity)
        self.assertFalse(missing.exists())
        with self.assertRaises(WorkItemValidationError):
            messaging.respond(config, self.reply)
        self.assertFalse(missing.exists())

    def test_local_path_redaction_preserves_envelope_structure(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            metadata = {**self.question["metadata"], "choices": [{"id": "one", "label": "/Users/operator/private"}]}
            conn.execute(
                "UPDATE work_item_events SET body=?,metadata_json=? WHERE event_id=?",
                ("Use /Users/operator/private", json.dumps(metadata), self.question["event_id"]),
            )
        message = messaging.inbox(self.config, self.identity)["messages"][0]
        self.assertEqual(message["summary"], "Use <local-path>")
        self.assertEqual(message["choices"], [{"id": "one", "label": "<local-path>"}])
        self.assertTrue(messaging.respond(self.config, self.reply)["ok"])

    def test_delivery_updates_preserve_a_concurrent_acknowledgement(self):
        from robert_agent.persistence import statements

        # Commit an operator acknowledgement just before the sender's write.
        # This reproduces a separate CLI writer winning the race after a read.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE notifications SET status='resolved'")
        notifications.record_notification(self.db_path, "supervision_incident", "recorded")
        with closing(sqlite3.connect(self.db_path)) as conn:
            identifier = conn.execute(
                "SELECT notification_id FROM notifications WHERE notification_type='supervision_incident'"
            ).fetchone()[0]
        request = {**self.reply, "notification_id": identifier, "version": 0, "action": "ack", "body": "Received."}
        write = statements.write
        acknowledged = False

        def interleave(conn, stmt, parameters=()):
            nonlocal acknowledged
            if not acknowledged:
                acknowledged = True
                self.assertEqual(messaging.respond(self.config, request)["status"], "acknowledged")
            return write(conn, stmt, parameters)

        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True), patch.object(statements, "write", side_effect=interleave):
            self.assertEqual(notifications.deliver_pending(self.db_path, config=self.config, runner=runner)["sent"], 1)
        self.assertTrue(acknowledged)
        with closing(sqlite3.connect(self.db_path)) as conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM notifications WHERE notification_id=?", (identifier,)
                ).fetchone()[0]
            )
        self.assertEqual(metadata["operator_response"]["body"], "Received.")
        self.assertEqual(metadata["delivery"]["status"], "sent")

    def test_admission_incident_keeps_repository_scope(self):
        from robert_agent.persistence import database
        from robert_agent.supervision import store
        from robert_agent.supervision.application import checks

        self.config["repos"].append({"full_name": "other/repo"})
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE notifications SET status='resolved'")
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES ('repo-other','other/repo','robot','main','/other','/other-worktrees')"
            )
        now = "2026-11-07T12:00:00Z"
        for scope_type in ("github_event", "work_item_event"):
            for scope_id, repo_id in (("denied", "repo-other"), ("allowed", "repo-1"), ("unknown", None)):
                with closing(database.connect(self.db_path)) as conn, conn:
                    if repo_id:
                        store.update_checkpoint(
                            conn, scope_type, scope_id, "admission", now=now, metadata={"repo_id": repo_id}
                        )
                    checks.notify_changes(
                        conn,
                        [
                            {
                                "transition": "opened",
                                "incident": {
                                    "incident_id": scope_type + "-" + scope_id,
                                    "scope_type": scope_type,
                                    "scope_id": scope_id,
                                    "rule_id": "admission_unobservable",
                                },
                            }
                        ],
                        now,
                    )
        inbox = messaging.inbox(self.config, self.identity)["messages"]
        self.assertEqual({row["incident_id"] for row in inbox}, {"github_event-allowed", "work_item_event-allowed"})
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(notifications.deliver_pending(self.db_path, config=self.config, runner=runner)["sent"], 2)
        for scope_type in ("github_event", "work_item_event"):
            for scope_id in ("denied", "unknown"):
                with self.assertRaises(WorkItemNotFoundError):
                    messaging.respond(
                        self.config,
                        {
                            **self.reply,
                            "action": "ack",
                            "version": 0,
                            "notification_id": f"supervision-{scope_type}-{scope_id}-opened",
                        },
                    )

    def test_legacy_admission_notices_resolve_scope_from_durable_incident(self):
        from robert_agent.persistence import database
        from robert_agent.supervision import store

        now = "2026-11-07T12:00:00Z"
        scopes = [
            ("allowed", "github_event", "repo-1"),
            ("denied", "work_item_event", "other"),
            ("missing", "github_event", None),
            ("global", "controller", None),
        ]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE notifications SET status='resolved'")
            for identifier, scope_type, _ in scopes:
                conn.execute(
                    "INSERT INTO supervision_incidents(incident_id,scope_type,scope_id,rule_id,status,reason_code,"
                    "severity,evidence_json,expected_progress_json,first_observed_at,last_observed_at) "
                    "VALUES (?,?,?,'stalled','open','stalled','warning','{}','{}',?,?)",
                    (identifier, scope_type, identifier, now, now),
                )
        with closing(database.connect(self.db_path)) as conn, conn:
            for identifier, scope_type, repo_id in scopes:
                if repo_id:
                    store.update_checkpoint(
                        conn, scope_type, identifier, "admission", now=now, metadata={"repo_id": repo_id}
                    )
        for identifier, _, _ in [*scopes, ("deleted", None, None)]:
            notifications.record_notification(
                self.db_path, "supervision_incident", "recorded", {"incident_id": identifier, "reason_code": "stalled"}
            )
        inbox = messaging.inbox(self.config, self.identity)["messages"]
        self.assertEqual({row["incident_id"] for row in inbox}, {"allowed", "global"})
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(notifications.deliver_pending(self.db_path, config=self.config, runner=runner)["sent"], 2)
        with closing(sqlite3.connect(self.db_path)) as conn:
            denied = [
                row[0]
                for row in conn.execute(
                    "SELECT notification_id FROM notifications WHERE json_extract(metadata_json,'$.incident_id') "
                    "IN ('denied','missing','deleted')"
                )
            ]
        for identifier in denied:
            with self.assertRaises(WorkItemNotFoundError):
                messaging.respond(
                    self.config, {**self.reply, "notification_id": identifier, "action": "ack", "version": 0}
                )

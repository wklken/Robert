"""Native notification visibility through authenticated messaging boundaries."""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import subprocess
from unittest.mock import Mock, patch

from robert_agent.delivery import messages
from robert_agent.delivery.application.notifications import deliver_pending
from robert_agent.execution.notifications import has_pending_local_delivery
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.stores import notifications
from robert_agent.work import messaging as work_messaging
from robert_agent.work.application import commands, messaging
from robert_agent.work.errors import WorkItemConflictError, WorkItemNotFoundError, WorkItemValidationError
from tests.support.source_cycle import SourceCycleFixture


class NativeNotificationScopeTests(SourceCycleFixture):
    def configure_messaging(self, *, two=False):
        self.config(development=True, two=two)
        self.request = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "conversation": "telegram:42",
            "thread": "",
            "account": "test-account",
            "sender_id": "42",
        }
        self.configured["messaging"] = {
            **{key: val for key, val in self.request.items() if key not in ("protocol", "sender_id")},
            "enabled": True,
            "allowed_senders": ["42", "43"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": ["org/alpha"],
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.configured["db_path"] = str(self.db)
        self.configured["tapd"]["projects"][0]["operators"] = ["openclaw:telegram:test-account:42"]

    def start_native(self):
        self.tick("admitted", ticket=self.ticket)
        prepared = self.tick("prepared", ticket=self.ticket)
        self.tick("dispatched", ticket=self.ticket)
        return prepared.identity

    def notice(self, notification_id, bound_task=None, *, at="2026-11-07T12:00:00Z", **metadata):
        with closing(database.connect(self.db)) as conn, conn:
            notifications.record_notification(
                conn,
                notification_id,
                bound_task,
                "worker_stale",
                "local",
                "recorded",
                at,
                json.dumps(metadata),
            )

    def test_same_repo_unauthorized_operator_cannot_see_native_task_notice(self):
        self.configure_messaging()
        task = self.start_native()
        self.notice("native", task)
        self.configured["tapd"]["enabled"] = False
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE repos SET enabled=0 WHERE repo_id='repo:org/alpha'")
        allowed = messaging.inbox(self.configured, self.request)["messages"]
        self.assertEqual([row["notification_id"] for row in allowed], ["native"])
        self.assertEqual(messaging.inbox(self.configured, {**self.request, "sender_id": "43"})["messages"], [])
        with closing(database.connect(self.db)) as conn:
            self.assertIsNone(messages.envelope(conn, "native"))
            self.assertIsNone(messages.envelope(conn, "native", config=self.configured))
            self.assertIsNotNone(messages.envelope(conn, "native", config=self.configured, delivery=True))

    def test_scope_before_inbox_delivery_limit_and_readiness(self):
        self.configure_messaging(two=True)
        task = self.start_native()
        # A same-repo project/destination mismatch excludes each row at selection.
        outsider = deepcopy(self.configured)
        other = deepcopy(outsider["tapd"]["projects"][0])
        other["workspace_id"] = "002"
        outsider["tapd"]["projects"].append(other)
        outsider["messaging"]["tapd_projects"][0]["workspace_id"] = "002"
        for index in range(105):
            self.notice(f"early-hidden-{index}", task, at="2026-11-06T12:00:00Z")
            self.notice(f"late-hidden-{index}", task, at="2026-11-08T12:00:00Z")
        now = "2026-11-09T12:00:00Z"
        for config in (
            outsider,
            {**self.configured, "messaging": {**self.configured["messaging"], "repos": ["org/beta"]}},
            {**self.configured, "messaging": {**self.configured["messaging"], "enabled": False}},
            None,
        ):
            with self.subTest(config=config is not None), closing(database.connect(self.db)) as conn:
                self.assertFalse(has_pending_local_delivery(conn, None, now, config=config))
        self.assertEqual(messaging.inbox(outsider, self.request)["messages"], [])
        self.notice("global")
        self.assertEqual(
            [message["notification_id"] for message in messaging.inbox(outsider, self.request)["messages"]],
            ["global"],
        )
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(deliver_pending(self.db, config=outsider, runner=runner, limit=1, now=now)["sent"], 1)
        self.assertEqual(runner.call_count, 1)
        text = runner.call_args.args[0]
        self.assertIn("global", text[text.index("--message") + 1])
        self.assertNotIn("early-hidden", text[text.index("--message") + 1])
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(has_pending_local_delivery(conn, None, now, config=outsider))
            self.assertTrue(has_pending_local_delivery(conn, None, now, config=self.configured))
        # A destination may receive an authorized scope without conferring
        # operator access on an authenticated sender.
        self.configured["tapd"]["projects"][0]["operators"] = []
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                deliver_pending(self.db, config=self.configured, runner=runner, limit=1, now=now)["sent"], 1
            )
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(
            [message["notification_id"] for message in messaging.inbox(self.configured, self.request)["messages"]],
            ["global"],
        )

    def test_persisted_item_and_task_targets_win_over_metadata_claims(self):
        self.configure_messaging()
        task = self.start_native()
        [[item, stream]] = self.read("SELECT work_item_id,workstream_id FROM work_items")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """INSERT INTO work_items(work_item_id,repo_id,title,origin_type,creation_idempotency_key,
                                          created_by,created_at,updated_at)
                   VALUES('other-item','repo:org/alpha','Unrelated same-repo private title','web',
                          'other-item','fixture',?,?)""",
                (utc_now(), utc_now()),
            )
        self.notice("task-with-original-item", task, work_item_id=item)
        self.notice("original-item", work_item_id=item)
        self.notice("wrong-item", task, work_item_id="other-item")
        self.notice("task-spoof", task, **{"task_id": "another-task"})
        self.notice("stream-spoof", task, workstream_id="other-stream")
        self.notice("conflicting-source", task, source_id=self.ticket.source_id, tapd_source_id="elsewhere")
        self.notice("source-only", source_id=self.ticket.source_id)
        self.notice("stream-only", workstream_id=stream)
        self.notice("workspace-only", target={"provider": "tapd", "workspace_id": self.ticket.workspace_id})
        entries = messaging.inbox(self.configured, self.request)["messages"]
        self.assertCountEqual(
            [entry["notification_id"] for entry in entries], ["task-with-original-item", "original-item"]
        )
        with closing(database.connect(self.db)) as conn:
            for denied in (
                "wrong-item",
                "task-spoof",
                "stream-spoof",
                "conflicting-source",
                "source-only",
                "stream-only",
            ):
                self.assertEqual(work_messaging.notification_scope(conn, denied).kind, "denied")
            for allowed in ("task-with-original-item", "original-item"):
                scope = work_messaging.notification_scope(conn, allowed)
                self.assertEqual(
                    (scope.kind, scope.source_id, scope.repo_id), ("tapd", self.ticket.source_id, "repo:org/alpha")
                )

    def test_ack_writer_rechecks_project_scope_and_never_executes_native_commands(self):
        self.configure_messaging()
        task = self.start_native()
        self.notice("native", task)
        reply = {**self.request, "notification_id": "native", "version": 0, "action": "ack"}
        with self.assertRaises(WorkItemNotFoundError):
            messaging.respond(self.configured, {**reply, "sender_id": "43"})
        with patch.object(messaging.commands, "execute_command", side_effect=AssertionError("unsafe legacy command")):
            for action in ("approve", "reply", "retry", "retry_publish", "cancel"):
                with self.assertRaisesRegex(WorkItemValidationError, "Native notification commands"):
                    messaging.respond(self.configured, {**reply, "action": action})
        with self.assertRaises(WorkItemConflictError):
            messaging.respond(self.configured, {**reply, "version": 1})
        project = self.configured["tapd"]["projects"][0]
        permitted = project["operators"]
        begin = transactions.begin_immediate

        def revoke_in_writer(conn):
            begin(conn)
            project["operators"] = []

        with patch.object(transactions, "begin_immediate", side_effect=revoke_in_writer):
            with self.assertRaises(WorkItemNotFoundError):
                messaging.respond(self.configured, reply)
        [[before]] = self.read("SELECT metadata_json FROM notifications WHERE notification_id='native'")
        self.assertNotIn("operator_response", json.loads(before))
        project["operators"] = permitted
        result = messaging.respond(self.configured, reply)
        self.assertEqual(messaging.respond(self.configured, reply), result)
        [[status, responded]] = self.read(
            "SELECT status,metadata_json FROM notifications WHERE notification_id='native'"
        )
        self.assertEqual(status, "recorded")
        self.assertEqual(json.loads(responded)["operator_response"]["actor_identity"], permitted[0])
        self.assertEqual(self.read("SELECT lifecycle FROM tasks WHERE task_id=?", (task,)), [("running",)])

    def test_native_attention_has_only_ack_even_when_existing_event_offers_approve(self):
        self.configure_messaging()
        task = self.start_native()
        [[item]] = self.read("SELECT work_item_id FROM work_items")
        with closing(database.connect(self.db)) as conn, conn:
            commands.record_system_event(
                conn,
                item,
                event_type="operator_decision_required",
                body="Review exact source scope",
                idempotency_key="question",
                metadata={"task_id": task},
                now=utc_now(),
            )
        [message] = messaging.inbox(self.configured, self.request)["messages"]
        self.assertEqual(message["actions"], ["ack"])
        self.assertEqual(message["summary"], "Review exact source scope")

    def test_adopted_stream_scope_depends_on_each_task_and_preserves_ordinary_history(self):
        self.configure_messaging()
        native = self.start_native()
        [[native_item, origin]] = self.read("SELECT work_item_id,workstream_id FROM work_items")
        # Recreate the stored association independently from GitHub publishing.
        # The existing c2a tests exercise the canonical adoption producer.
        now = utc_now()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                """INSERT INTO sources(source_id,repo_id,source_key,source_type,provider,host,project_key,
                                       external_id,number,title,state,source_updated_at)
                   VALUES('pr','repo:org/alpha','pr-key','pull_request','github','github.com','org/alpha',
                          '17',17,'Ordinary PR title','open',?)""",
                (now,),
            )
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,
                                           created_at,updated_at)
                   VALUES('adopted','repo:org/alpha','pr',?,?,?)""",
                (origin, now, now),
            )
            conn.execute("INSERT INTO workstream_sources VALUES('adopted','pr','derived_pr',?)", (now,))
            conn.execute(
                """INSERT INTO work_items(work_item_id,repo_id,title,origin_type,origin_source_id,workstream_id,
                                          creation_idempotency_key,created_by,created_at,updated_at)
                   VALUES('gh-item','repo:org/alpha','Historical PR work item','github','pr','adopted',
                          'gh-item','fixture',?,?)""",
                (now, now),
            )
            conn.execute(
                """INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)
                   VALUES('historical-gh','adopted','completed',?,?)""",
                (now, now),
            )
            conn.execute("UPDATE tasks SET workstream_id='adopted' WHERE task_id=?", (native,))
        self.notice("derived-with-gh-item", native, work_item_id="gh-item", workstream_id="adopted")
        self.notice("derived-with-origin-item", native, work_item_id=native_item, workstream_id=origin)
        self.notice("legacy-gh", "historical-gh", work_item_id="gh-item")
        self.notice("wrong-native-item", "historical-gh", work_item_id=native_item)
        self.assertCountEqual(
            [row["notification_id"] for row in messaging.inbox(self.configured, self.request)["messages"]],
            ["derived-with-gh-item", "derived-with-origin-item", "legacy-gh"],
        )
        with closing(database.connect(self.db)) as conn:
            self.assertEqual(work_messaging.notification_scope(conn, "derived-with-origin-item").kind, "tapd")
            self.assertEqual(work_messaging.notification_scope(conn, "legacy-gh").kind, "repository")
        other_actor = {**self.request, "sender_id": "43"}
        self.assertEqual(
            [row["notification_id"] for row in messaging.inbox(self.configured, other_actor)["messages"]], ["legacy-gh"]
        )
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DELETE FROM source_round_tasks WHERE task_id=?", (native,))
        with closing(database.connect(self.db)) as conn:
            self.assertEqual(work_messaging.notification_scope(conn, "derived-with-gh-item").kind, "denied")
        self.assertEqual(
            [row["notification_id"] for row in messaging.inbox(self.configured, self.request)["messages"]],
            ["legacy-gh"],
        )

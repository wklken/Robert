"""Exercise source-owned attention and receipts through the actual coordinator."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest.mock import Mock, patch

from robert_agent.application import source_cycle
from robert_agent.delivery import messages
from robert_agent.delivery.application.notifications import deliver_pending
from robert_agent.execution.notifications import has_pending_local_delivery
from robert_agent.foundation.values import utc_now
from robert_agent.persistence import database, transactions
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import notifications, source_attention
from robert_agent.work.application import messaging, source_observation, source_preparation, tapd_intake
from robert_agent.work.errors import WorkItemConflictError, WorkItemNotFoundError
from tests.support.source_cycle import SourceCycleFixture


class SourceAttentionTests(SourceCycleFixture):
    def configure(self, *, two=True):
        self.config(two=two)
        self.identity = {
            "protocol": "robert.message.v1",
            "provider": "openclaw",
            "channel": "telegram",
            "conversation": "telegram:42",
            "thread": "",
            "account": "test-account",
            "sender_id": "42",
        }
        self.configured["messaging"] = {
            **self.identity,
            "enabled": True,
            "allowed_senders": ["42", "43"],
            "send_after": "2026-07-01T00:00:00Z",
            "target": "42",
            "repos": [],
            "tapd_projects": [{"host": self.ticket.host, "workspace_id": self.ticket.workspace_id}],
        }
        self.configured["db_path"] = str(self.db)
        self.configured["tapd"]["projects"][0]["operators"] = ["openclaw:telegram:test-account:42"]

    def test_preselection_failure_produces_one_project_notification_without_task(self):
        self.configure()
        self.tick("admitted", ticket=self.ticket)
        self.model_unavailable = True
        self.assertEqual(self.tick("attention", ticket=self.ticket).reason, "unsupported_model_profile")
        [entry] = messaging.inbox(self.configured, self.identity)["messages"]
        self.assertIsNone(entry["task_id"])
        self.assertEqual(entry["actions"], ["ack"])
        self.assertEqual(entry["reason_code"], "unsupported_model_profile")
        self.assertEqual(self.read("SELECT COUNT(*) FROM work_items"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(messaging.inbox(self.configured, {**self.identity, "sender_id": "43"})["messages"], [])

    def observe(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            return tapd_intake.observe_ticket(
                conn,
                self.configured["tapd"],
                self.provider(None, self.ticket),
                source_observation.publication_facts(conn, self.ticket),
                now=utc_now(),
            )

    def emit_preselection(self):
        self.tick("admitted", ticket=self.ticket)
        self.model_unavailable = True
        self.tick("attention", ticket=self.ticket)
        [entry] = messaging.inbox(self.configured, self.identity)["messages"]
        return entry

    def ack(self, entry, **kwargs):
        return messaging.respond(
            self.configured,
            {
                **self.identity,
                "notification_id": entry["notification_id"],
                "version": entry["version"],
                "action": "ack",
                **kwargs,
            },
        )

    def test_ack_remains_visible_repeated_poll_dedups_and_cleared_cause_recurs(self):
        self.configure()
        old = self.emit_preselection()
        self.assertEqual(old["target"]["source_id"], self.ticket.source_id)
        self.assertNotIn("repo_id", old["target"])
        self.assertEqual(old["version"], 1)
        with self.assertRaises(WorkItemConflictError):
            self.ack(old, version=3)
        with self.assertRaises(WorkItemNotFoundError):
            self.ack(old, sender_id="43")
        project = self.configured["tapd"]["projects"][0]
        initial_operators = project["operators"]
        begin = transactions.begin_immediate

        def revoke(conn):
            begin(conn)
            project["operators"] = []

        with (
            patch.object(transactions, "begin_immediate", side_effect=revoke),
            self.assertRaises(WorkItemNotFoundError),
        ):
            self.ack(old)
        project["operators"] = initial_operators
        self.assertEqual(self.ack(old), self.ack(old))
        init_database(self.db)
        self.tick("attention", ticket=self.ticket)
        self.assertEqual(self.read("SELECT version,resolved_at FROM source_attentions"), [(1, None)])
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("active",)])
        [again] = messaging.inbox(self.configured, self.identity)["messages"]
        self.assertEqual(again["notification_id"], old["notification_id"])

        self.terminal = True
        self.observe()
        self.tick("canceled", ticket=self.ticket)
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])
        self.terminal = False
        self.comments = [self.comment("2")]
        new = self.emit_preselection()
        self.assertNotEqual(new["notification_id"], old["notification_id"])
        self.assertEqual(new["version"], 2)
        self.assertEqual(self.ack(old)["status"], "acknowledged")
        [[payload]] = self.read(
            "SELECT metadata_json FROM notifications WHERE notification_id=?", (old["notification_id"],)
        )
        self.assertIn("operator_response", json.loads(payload))

    def test_known_preparation_repo_without_task_still_requires_repo_scope(self):
        self.configure(two=False)
        self.tick("admitted", ticket=self.ticket)
        with patch.object(source_preparation, "prepare_source_task", return_value=None):
            result = self.tick("attention", ticket=self.ticket)
        self.assertEqual(result.reason, "source_preparation_failed")
        self.assertEqual(
            self.read("SELECT repo_id,repo_basis,task_id FROM source_attentions"), [("repo:org/alpha", "round", None)]
        )
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])
        self.configured["messaging"]["repos"] = ["org/alpha"]
        [entry] = messaging.inbox(self.configured, self.identity)["messages"]
        self.assertEqual(entry["target"]["repo_id"], "repo:org/alpha")
        self.assertEqual(self.read("SELECT COUNT(*) FROM tasks"), [(0,)])

    def test_failed_outbox_transaction_rolls_back_progress_and_attention(self):
        self.configure()
        self.tick("admitted", ticket=self.ticket)
        before = self.read("SELECT status,reason,round_id FROM tapd_coordination")
        self.model_unavailable = True
        record = source_attention.open_coordination

        def fault(conn, **options):
            record(conn, **options)
            raise RuntimeError("after outbox pairing")

        with patch.object(source_attention, "open_coordination", side_effect=fault):
            with self.assertRaisesRegex(RuntimeError, "after outbox pairing"):
                self.tick("attention", ticket=self.ticket)
        self.assertEqual(self.read("SELECT status,reason,round_id FROM tapd_coordination"), before)
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_attentions"), [(0,)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM notifications"), [(0,)])
        self.tick("attention", ticket=self.ticket)
        self.assertEqual(self.read("SELECT version,resolved_at FROM source_attentions"), [(1, None)])

    def test_project_only_proof_filters_forged_targets_before_limit_and_delivery_lane(self):
        self.configure()
        entry = self.emit_preselection()
        pub_id = entry["notification_id"]
        with closing(database.connect(self.db)) as conn, conn:
            for index in range(105):
                for date in ("2026-11-07T12:00:00Z", "2029-11-07T12:00:00Z"):
                    notifications.record_notification(
                        conn,
                        f"forged-{index}-{date}",
                        None,
                        "source_coordination_attention",
                        "local",
                        "recorded",
                        date,
                        json.dumps({"source_attention_id": pub_id}),
                    )
        self.configured["tapd"]["enabled"] = False
        self.assertEqual(
            [msg["notification_id"] for msg in messaging.inbox(self.configured, self.identity)["messages"]],
            [pub_id],
        )
        now = "2030-11-07T12:00:00Z"
        lane = ["repo:unrelated"]
        with closing(database.connect(self.db)) as conn:
            self.assertIsNone(messages.envelope(conn, pub_id, config=self.configured))
            self.assertFalse(has_pending_local_delivery(conn, lane, now))
            self.assertTrue(has_pending_local_delivery(conn, lane, now, self.configured))
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
        with patch.dict("os.environ", {}, clear=True):
            result = deliver_pending(self.db, config=self.configured, runner=runner, repo_ids=lane, limit=1, now=now)
        self.assertEqual(result["sent"], 1)
        runner.assert_called_once()
        self.assertIn(pub_id, runner.call_args.args[0][runner.call_args.args[0].index("--message") + 1])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE notifications SET metadata_json=json_set(metadata_json,'$.source_id','fake-source')"
                " WHERE notification_id=?",
                (pub_id,),
            )
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])
        with self.assertRaises(WorkItemNotFoundError):
            self.ack(entry)

    def test_unbound_stage_identity_keeps_local_progress_without_outbox(self):
        self.configure()
        active = self.tick("admitted", ticket=self.ticket)
        invalid = source_cycle.SourceStep(
            "attention", self.ticket.source_id, active.round_id, "source_stage_failed", "unbound-external-run"
        )
        with patch.object(source_cycle, "_advance", return_value=invalid):
            self.tick("attention", ticket=self.ticket)
        self.assertEqual(
            self.read("SELECT status,reason FROM tapd_coordination"), [("attention", "source_stage_failed")]
        )
        self.assertEqual(self.read("SELECT COUNT(*) FROM source_attentions"), [(0,)])
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])

    def test_additive_install_preserves_preexisting_notification(self):
        self.configure()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE source_attentions")
            conn.execute(
                "INSERT INTO notifications VALUES('legacy',NULL,'worker_stale','local','recorded',?,'{}')",
                (utc_now(),),
            )
        init_database(self.db)
        self.assertEqual(
            [msg["notification_id"] for msg in messaging.inbox(self.configured, self.identity)["messages"]], ["legacy"]
        )
        self.assertEqual(self.read("PRAGMA foreign_key_check"), [])

    def test_selected_repo_is_bound_before_first_preparation_from_multiple_candidates(self):
        self.configure()
        self.tick("admitted", ticket=self.ticket)
        selected = self.tick("stage_recorded", ticket=self.ticket)
        with patch.object(source_preparation, "prepare_source_task", return_value=None):
            self.tick("attention", ticket=self.ticket)
        [[repo, basis, run]] = self.read("SELECT repo_id,repo_basis,model_run_id FROM source_attentions")
        self.assertEqual((basis, run), ("selection", selected.identity))
        self.assertEqual(self.read("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])
        self.configured["messaging"]["repos"] = [repo.removeprefix("repo:")]
        [notice] = messaging.inbox(self.configured, self.identity)["messages"]
        self.assertEqual(notice["target"]["repo_id"], repo)

    def test_actual_worker_attempt_attention_requires_its_current_bound_repo(self):
        self.configure()
        self.tick("admitted", ticket=self.ticket)
        self.tick("stage_recorded", ticket=self.ticket)
        task = self.tick("prepared", ticket=self.ticket)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE execution_subjects SET metadata_json=json_set(metadata_json,'$.dispatch_inflight','fixture')"
                " WHERE subject_type='task' AND subject_id=?",
                (task.identity,),
            )
        self.terminal = True
        self.observe()
        held = self.tick("attention", ticket=self.ticket)
        self.assertEqual(held.reason, "worker_launch_unconfirmed")
        self.assertEqual(held.identity, task.identity)
        [[repo, task_id, attempt_id]] = self.read("SELECT repo_id,task_id,attempt_id FROM source_attentions")
        self.assertEqual((task_id, attempt_id), (task.identity, None))
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])
        self.configured["messaging"]["repos"] = ["org/alpha", "org/beta"]
        [notice] = messaging.inbox(self.configured, self.identity)["messages"]
        self.assertEqual(notice["target"]["repo_id"], repo)
        self.ack(notice)
        self.assertEqual(self.read("SELECT status FROM attempts"), [("prepared",)])
        wrong = "repo:org/beta" if repo == "repo:org/alpha" else "repo:org/alpha"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_attentions SET repo_id=?", (wrong,))
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])

    def test_model_exit_attention_still_occupies_saved_process_after_ack(self):
        self.configure()
        self.stop_model = True
        self.tick("admitted", ticket=self.ticket)
        self.tick("attention", ticket=self.ticket)
        [[run, reserved]] = self.read("SELECT run_id,slot_reserved FROM source_model_runs")
        self.assertEqual(reserved, 1)
        self.assertEqual(self.read("SELECT model_run_id,repo_id FROM source_attentions"), [(run, None)])
        [notice] = messaging.inbox(self.configured, self.identity)["messages"]
        self.ack(notice)
        self.assertEqual(self.read("SELECT slot_reserved FROM source_model_runs"), [(1,)])
        self.comments = [self.comment("1", marked=False)]
        self.tick("attention", ticket=self.ticket, terminate_model=lambda _: {"status": "attention"})
        self.assertEqual(self.read("SELECT version,resolved_at FROM source_attentions"), [(1, None)])
        self.tick("draining", ticket=self.ticket, terminate_model=lambda _: {"status": "terminated"})
        self.assertEqual(self.read("SELECT slot_reserved FROM source_model_runs"), [(0,)])
        self.assertEqual(messaging.inbox(self.configured, self.identity)["messages"], [])

from contextlib import closing
from datetime import timedelta
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import publication_budget as publication_fixture
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.delivery import values as publication_values
from robert_agent.delivery.application import publish
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision import actions as supervision_actions
from robert_agent.supervision.application import publication as supervision_publication


class SupervisedPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = publication_fixture.PublicationBudgetFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db, self.now = fixture.db, fixture.now
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE workstreams SET active_task_id='t'")
            conn.execute("""UPDATE tasks SET metadata_json=json_object(
                'supervision_lineage_key','lineage','trigger_work_item_event_id','operator-event')""")
            execution_store.ensure_subject(conn, "task", "t", "human_request", self.now.isoformat())
        self.config = {
            "repos": [{"full_name": "x/y"}],
            "supervision": {
                "mode": "recover_known",
                "recovery": {
                    "repositories": ["x/y"],
                    "allowed_actions": ["publication_retry", "publication_reconcile"],
                },
            },
        }
        self.commands = []
        self.remote = ""
        self.error = "connection reset by peer"

    def runner(self, command, **kwargs):
        self.commands.append(command[:2])
        if command[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout=self.remote, stderr="")
        if command[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=int(bool(self.error)), stdout="", stderr=self.error)
        raise AssertionError(command)

    def cycle(self, seconds=0, config=None):
        self.now += timedelta(seconds=seconds)
        with mock.patch.object(publication_values, "now", return_value=self.now.isoformat()):
            kwargs = {"config": config} if config is not None else {}
            return publish.publish_ready_actions(self.db, run_command=self.runner, **kwargs)

    def intent(self):
        with closing(sqlite3.connect(self.db)) as conn:
            status, raw = conn.execute("SELECT status,remote_evidence_json FROM publication_intents").fetchone()
        return status, json.loads(raw)

    def actions(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT action_kind,status,budget_cost FROM supervision_recovery_actions ORDER BY rowid"
            ).fetchall()

    def verify(self, seconds=60):
        self.now += timedelta(seconds=seconds)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return supervision_actions.verify(conn, observations=[], now=self.now.isoformat())

    def request(self, kind, seconds=0):
        self.now += timedelta(seconds=seconds)
        reconcile = kind == "publication_reconcile"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            requested = supervision_actions.plan(
                conn,
                config=self.config,
                now=self.now.isoformat(),
                observations=[
                    {
                        "scope_type": "task",
                        "scope_id": "t",
                        "lineage_key": "lineage",
                        "classification": "confirmed",
                        "rule_id": "publication_stalled",
                        "reason_code": "publication_effect_unresolved" if reconcile else "publication_waiting_consumer",
                        "evidence": {
                            "action_id": "push",
                            "intent_status": "executing" if reconcile else "confirmed_absent",
                        },
                    }
                ],
            )
        self.assertEqual(len(requested), 1)

    def test_native_publication_preserves_original_retry_budget(self):
        for seconds in (0, 60, 180, 600):
            self.cycle(seconds, self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 3)
        self.assertEqual(self.actions(), [])

    def test_unmanaged_gate_does_not_take_a_writer_lock(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            action = dict(conn.execute("SELECT * FROM github_actions").fetchone())
        ready, release = threading.Event(), threading.Event()

        def hold():
            with closing(sqlite3.connect(self.db)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                ready.set()
                release.wait(5)
                writer.rollback()

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(ready.wait(5))
        try:
            started = time.monotonic()
            verdict = supervision_publication.begin(self.db, action, None, self.now.isoformat())
            self.assertEqual(verdict["status"], "legacy")
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            thread.join(5)

    def test_gate_database_error_defers_without_charging_and_continues_other_task(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for table, old, new, key in [
                ("workstreams", "ws", "ws2", "workstream_id"),
                ("tasks", "t", "t2", "task_id"),
                ("attempts", "a", "a2", "attempt_id"),
                ("worker_results", "r", "r2", "result_id"),
                ("github_actions", "push", "push2", "action_id"),
            ]:
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                row = dict(
                    zip(
                        columns,
                        conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (old,)).fetchone(),
                    )
                )
                row[key] = new
                for field, value in [
                    ("task_id", "t2"),
                    ("workstream_id", "ws2"),
                    ("attempt_id", "a2"),
                    ("result_id", "r2"),
                    ("active_task_id", "t2"),
                ]:
                    if field in row:
                        row[field] = value
                conn.execute(
                    f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                    list(row.values()),
                )
        original = supervision_publication.begin

        def gate(db, action, config, now):
            if action["task_id"] == "t":
                raise sqlite3.OperationalError("database is locked")
            return original(db, action, config, now)

        self.error = ""
        with mock.patch.object(supervision_publication, "begin", side_effect=gate):
            report = self.cycle()
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(report["deferred_count"], 1)
        self.assertEqual(report["published_count"], 1)
        with closing(sqlite3.connect(self.db)) as conn:
            state, raw = conn.execute(
                "SELECT publish_status,metadata_json FROM github_actions WHERE action_id='push'"
            ).fetchone()
            self.assertEqual(state, "not_published")
            self.assertNotIn("publish", json.loads(raw))
            self.assertFalse(conn.execute("SELECT 1 FROM publication_intents WHERE action_id='push'").fetchone())

    def test_absence_reconciliation_never_writes_until_new_claim(self):
        self.cycle()
        self.assertEqual(self.commands.count(["git", "push"]), 1)
        self.error = ""
        self.request("publication_reconcile", 60)
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 1)
        status, evidence = self.intent()
        self.assertEqual(status, "confirmed_absent")
        self.assertTrue(evidence["supervision_retry_required"])
        self.assertEqual(self.actions(), [("publication_reconcile", "observing", 1)])
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 1)
        self.verify()
        self.cycle()  # Legacy invocation cannot bypass the durable retry fence.
        self.assertEqual(self.commands.count(["git", "push"]), 1)
        self.request("publication_retry")
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 2)
        self.assertEqual(self.intent()[0], "published")
        self.verify()
        self.assertEqual(
            self.actions(),
            [
                ("publication_reconcile", "succeeded", 1),
                ("publication_retry", "succeeded", 1),
            ],
        )

    def test_confirmed_remote_effect_repairs_receipt_without_duplicate_push(self):
        self.cycle()
        self.remote = "b" * 40 + "\trefs/heads/codex/fix\n"
        self.request("publication_reconcile", 60)
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 1)
        self.assertEqual(self.intent()[0], "published")
        self.verify()
        self.assertEqual(self.actions(), [("publication_reconcile", "succeeded", 1)])

    def test_recovery_config_revoked_after_claim_prevents_push(self):
        self.cycle()
        self.request("publication_reconcile", 60)
        self.cycle(config=self.config)
        self.verify()
        self.error = ""
        self.request("publication_retry")
        original = supervision_publication.guard
        with mock.patch.object(
            supervision_publication,
            "guard",
            side_effect=lambda db, action, state, config, now: original(db, action, state, {}, now),
        ):
            self.cycle(config=self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 1)
        self.assertEqual(self.intent()[0], "confirmed_absent")
        self.assertEqual(self.actions()[-1], ("publication_retry", "rejected", 0))

    def test_crashed_claim_reconciles_same_action_and_retains_write_budget(self):
        self.cycle()
        self.request("publication_reconcile", 60)
        self.cycle(config=self.config)
        self.verify()
        self.request("publication_retry")
        original = self.runner

        def crash(command, **kwargs):
            if command[:2] == ["git", "push"]:
                self.commands.append(command[:2])
                raise RuntimeError("publisher crash during exact action")
            return original(command, **kwargs)

        with mock.patch.object(self, "runner", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "publisher crash"):
                self.cycle(config=self.config)
        self.assertEqual(self.actions()[-1], ("publication_retry", "applying", 1))
        self.cycle(60, self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 2)
        self.assertEqual(self.intent()[0], "confirmed_absent")
        self.assertEqual(self.actions()[-1], ("publication_retry", "failed", 1))
        self.error = ""
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 2)

    def test_unrequested_native_reconciliation_keeps_original_retry_authority(self):
        self.cycle()
        self.error = ""
        self.config["supervision"]["recovery"]["allowed_actions"] = ["publication_retry"]
        self.cycle(60, self.config)
        self.assertEqual(self.commands.count(["git", "push"]), 2)
        self.assertEqual(self.intent()[0], "published")
        self.assertEqual(self.actions(), [])

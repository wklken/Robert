from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.execution.application import replay as execution_replay
from robert_agent.runtime import loop as loop_engine
from robert_agent.runtime import cycle as run_once
from robert_agent.operations import migration as sqlite_migration
from tests.support import legacy_workflow_signals as workflow_signals
from tests.support import legacy_workflow_store as workflow_store
from tests.support import repository_cycle as test_run_once
from robert_agent.delivery.application import result_audit
from robert_agent.delivery.application import scope
from robert_agent.work.application import source_events


class ExecutionReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_run_once.RepositoryCycleFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        result = run_once.run_once(
            self.fixture.config_path,
            fixture_path=self.fixture.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        self.db = self.fixture.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.task, self.attempt, self.attempt_metadata = conn.execute(
                "SELECT task_id,attempt_id,metadata_json FROM attempts"
            ).fetchone()
            self.event = json.loads(conn.execute("SELECT payload_json FROM github_events LIMIT 1").fetchone()[0])
            conn.execute("UPDATE tasks SET lifecycle='waiting_for_user'")
            conn.execute("UPDATE workstreams SET lifecycle='waiting_for_user'")
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
            conn.execute("DELETE FROM execution_subjects")
            self.binding = workflow_store.bind_subject(
                conn,
                "task",
                self.task,
                "human_request",
                "dbos",
                "2026-10-24T00:00:00+00:00",
                definition_version="v2",
            )

    def migrate_replies(self, trusted=True):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for index in range(3):
                event = {
                    **self.event,
                    "event_fingerprint": f"reply-{index}",
                    "event_type": "comment",
                    "actor_login": "wklken" if trusted else "outsider",
                    "authorization_status": "accepted_context",
                    "drives_execution": index != 1,
                    "body": f"Reply {index}",
                }
                source_events.insert_source_and_event(conn, "repo:example/backend", event, "2026-10-24T00:00:00+00:00")
                workflow_signals.record_subject_signal(
                    conn,
                    self.binding,
                    "subject-events",
                    event["event_fingerprint"],
                    {"fact_id": event["event_fingerprint"]},
                    "2026-10-24T00:00:00+00:00",
                )
        plan = sqlite_migration.preview(self.db)
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(sqlite_migration.apply(self.db, plan)["ok"])

    def test_migration_reply_batch_preserves_context_once_after_restart(self):
        self.migrate_replies()
        for _ in range(2):
            result = run_once.run_once(
                self.fixture.config_path,
                dry_run=True,
                skip_external=True,
                skip_publish=True,
            )
            self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            child = conn.execute("SELECT task_id FROM tasks WHERE parent_task_id=?", (self.task,)).fetchone()[0]
            events = {
                row[0]
                for row in conn.execute(
                    "SELECT ge.event_fingerprint FROM task_events te JOIN github_events ge ON ge.event_id=te.event_id WHERE te.task_id=? AND ge.event_fingerprint LIKE 'reply-%'",
                    (child,),
                )
            }
            self.assertEqual(events, {"reply-0", "reply-1", "reply-2"})
            self.assertEqual(
                conn.execute("SELECT count(*) FROM execution_pending_facts WHERE consumed_at IS NULL").fetchone()[0],
                0,
            )

    def test_pending_facts_remain_schedulable_after_wakeup_consumed(self):
        self.migrate_replies()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE wakeups SET status='consumed'")
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_id="repo:example/backend"))

    def test_migrated_facts_wait_after_audit_retry_exhaustion(self):
        from robert_agent.delivery.application import submission as worker_result

        self.migrate_replies()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='running'")
            conn.execute("UPDATE workstreams SET lifecycle='active'")
            conn.execute(
                "UPDATE attempts SET status='prepared',metadata_json=?",
                (self.attempt_metadata,),
            )
        with (
            patch.object(
                worker_result.action_scope,
                "observe_worktree_status",
                return_value="clean",
            ),
            patch.object(worker_result.action_scope, "observe_head", return_value="saved-head"),
        ):
            receipt = worker_result.record_result(
                self.db,
                {
                    "task_id": self.task,
                    "attempt_id": self.attempt,
                    "output_type": "new_pr",
                    "submission": {
                        "planned_github_actions": test_run_once._new_pr_actions(),
                        "consumed_event_fingerprints": [self.event["event_fingerprint"]],
                        "verification": [test_run_once._verification_evidence()],
                        "used_skills": [],
                        "handoff": "ready",
                    },
                },
            )
        self.assertTrue(receipt["ok"], receipt)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            # Exhaust the real audit budget; only the external Git observation fails.
            for now in (
                "2026-11-06T00:00:00+00:00",
                "2026-11-06T00:00:10+00:00",
                "2026-11-06T00:00:30+00:00",
            ):
                with patch.object(
                    scope,
                    "for_result",
                    side_effect=subprocess.TimeoutExpired(["git", "status"], 1),
                ):
                    result_audit.audit_completed_results(
                        conn,
                        "repo:example/backend",
                        self.fixture.data_dir,
                        self.db,
                        {},
                        "audit-replay-test",
                        now,
                        True,
                        [],
                        {},
                    )
            metadata = json.loads(conn.execute("SELECT metadata_json FROM tasks").fetchone()[0])
            self.assertEqual(metadata["audit_retry_required"], receipt["result_id"])
            self.assertEqual(
                execution_store.get_binding(conn, "task", self.task)["binding_status"],
                "active",
            )
            conn.execute("UPDATE wakeups SET status='consumed'")
            facts = conn.execute("SELECT * FROM execution_pending_facts").fetchall()
            self.assertEqual(len(facts), 3)
        for _ in range(2):
            # A new connection models repeated daemon observations after restart.
            with closing(sqlite3.connect(self.db)) as conn:
                conn.execute("PRAGMA query_only=ON")
                self.assertFalse(
                    loop_engine.has_runnable_local_work(
                        conn,
                        repo_id="repo:example/backend",
                        now=now,
                    )
                )
                self.assertEqual(
                    conn.execute("SELECT * FROM execution_pending_facts").fetchall(),
                    facts,
                )
                self.assertIsNone(conn.execute("SELECT consumed_at FROM execution_pending_facts LIMIT 1").fetchone()[0])
        loop = loop_engine.run_loop(
            self.fixture.config_path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
        )
        self.assertEqual(loop["cycles"], 0)
        self.assertEqual(loop["stop_reason"], "no_runnable_work")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            # Once explicit recovery clears the fence, the same facts become runnable.
            conn.execute("UPDATE tasks SET metadata_json=json_remove(metadata_json,'$.audit_retry_required')")
            self.assertTrue(
                loop_engine.has_runnable_local_work(
                    conn,
                    repo_id="repo:example/backend",
                    now=now,
                )
            )

    def test_migrated_facts_resume_when_task_attention_is_cleared(self):
        self.migrate_replies()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE wakeups SET status='consumed'")
            conn.execute(
                "UPDATE tasks SET metadata_json=json_set(metadata_json,'$.execution_attention',json(?))",
                (json.dumps({"reason_code": "source_context_missing"}),),
            )
            facts = conn.execute("SELECT * FROM execution_pending_facts").fetchall()
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id="repo:example/backend"))
            conn.execute("UPDATE tasks SET metadata_json=json_remove(metadata_json,'$.execution_attention')")
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_id="repo:example/backend"))
            self.assertEqual(conn.execute("SELECT * FROM execution_pending_facts").fetchall(), facts)

    def test_untrusted_reply_stops_with_actionable_attention_without_busy_loop(self):
        self.migrate_replies(trusted=False)
        result = run_once.run_once(
            self.fixture.config_path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
        )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            control = execution_store.get_binding(conn, "task", self.task)
            self.assertEqual(control["binding_status"], "attention")
            self.assertIn("required_action", json.loads(control["metadata_json"]))
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id="repo:example/backend"))

    def test_stale_remediation_generation_consumed_without_new_attempt_or_budget_reset(
        self,
    ):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE workflow_bindings SET binding_status='terminal'")
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute(
                "INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,observed_head_sha,observed_base_sha,status,first_seen_at,updated_at,attempt_count,last_task_id) VALUES ('ep',?,'ci','pr7','head','base','open','now','now',1,?)",
                (self.event["workstream_id"], self.task),
            )
            conn.execute(
                "INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,recovery_attempts,created_at,updated_at) VALUES ('remediation_episode','ep','pr_remediation','active',1,'now','now')"
            )
            event = {
                **self.event,
                "event_fingerprint": "health",
                "authorization_status": "authorized_system_trigger",
                "metadata": {"remediation": {"episode_id": "ep"}},
            }
            conn.execute(
                "INSERT INTO execution_pending_facts(source_signal_id,subject_type,subject_id,topic,dedupe_key,fact_id,disposition,repo_id,task_id,payload_json,not_before_at) VALUES ('signal','remediation_episode','ep','remediation_decision','health:0','health','github_event','repo:example/backend',?,?,?)",
                (self.task, json.dumps(event), "2026-10-24T00:00:00+00:00"),
            )
            for _ in range(2):
                self.assertEqual(
                    execution_replay.decisions(conn, "repo:example/backend", "2026-10-24T01:00:00+00:00", []),
                    [],
                )
            self.assertEqual(
                conn.execute("SELECT attempt_count FROM pr_remediation_episodes").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT recovery_attempts FROM execution_subjects WHERE subject_id='ep'").fetchone()[0],
                1,
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertIsNotNone(conn.execute("SELECT consumed_at FROM execution_pending_facts").fetchone()[0])

    def test_canceled_subject_facts_are_disposed_without_reviving_work(self):
        self.migrate_replies()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled'")
            conn.execute("UPDATE execution_subjects SET binding_status='terminal'")
            self.assertEqual(
                execution_replay.decisions(conn, "repo:example/backend", "2026-10-24T01:00:00+00:00", []),
                [],
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM execution_pending_facts WHERE consumed_at IS NULL").fetchone()[0],
                0,
            )
            resolutions = [
                json.loads(row[0]) for row in conn.execute("SELECT resolution_json FROM execution_pending_facts")
            ]
            self.assertTrue(all(row["reason"] == "subject_terminal" for row in resolutions))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id="repo:example/backend"))

"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import (
    storage,
    publish,
    loop_engine,
    execution_operations,
    execution_store,
)


class PublicationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "db"
        self.now = datetime.now(timezone.utc)
        self.calls = []
        self.error = "permission denied"
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript(storage.schema_sql())
            c.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','x/y','bot','main','/repo','/worktrees')"
            )
            c.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('ws','repo','active','now','now')"
            )
            c.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('t','ws','running','now','now')"
            )
            c.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES ('a','t',1,'completed',json_object('result_contract','compact-v1'))"
            )
            scope = {
                "repo": "x/y",
                "worktree_path": self.tmp.name,
                "branch_name": "codex/fix",
                "remote": "origin",
                "result_head_sha": "b" * 40,
                "sources": [{"source_type": "issue", "number": 1}],
            }
            c.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES ('r','t','a','new_pr','now',?)",
                (json.dumps({"result_contract": "compact-v1", "audit": {"status": "accepted", "action_scope": scope}}),),
            )
            c.execute(
                "INSERT INTO github_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "push",
                    "r",
                    "t",
                    "push_existing_pr",
                    "",
                    "",
                    "accepted",
                    "not_published",
                    "now",
                    json.dumps(
                        {
                            "worktree_path": self.tmp.name,
                            "remote": "origin",
                            "branch": "codex/fix",
                        }
                    ),
                ),
            )

    def runner(self, command, **kwargs):
        self.calls.append(command[:2])
        if command[:2] == ["git", "ls-remote"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.error)
        raise AssertionError(command)

    def cycle(self, seconds=0):
        self.now += timedelta(seconds=seconds)
        with mock.patch.object(publish, "_now", return_value=self.now.isoformat()):
            return publish.publish_ready_actions(self.db, run_command=self.runner)

    def test_permanent_rejection_stops_remote_calls_and_scheduler(self):
        self.cycle()
        for _ in range(1000):
            self.assertEqual(self.cycle(600)["failed_count"], 0)
        self.assertEqual(self.calls.count(["git", "push"]), 1)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertFalse(loop_engine.has_runnable_local_work(c))
            self.assertEqual(
                c.execute("SELECT status FROM publication_intents").fetchone()[0],
                "attention",
            )

    def test_transient_rejection_backs_off_and_exhausts_budget(self):
        self.error = "connection reset by peer"
        self.cycle()
        self.assertEqual(self.cycle()["pending_count"], 0)
        for _ in range(8):
            self.cycle(60)
        self.assertEqual(self.calls.count(["git", "push"]), 3)

    def test_blocked_push_does_not_publish_later_comment(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "INSERT INTO github_actions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "comment",
                    "r",
                    "t",
                    "comment",
                    "https://github.com/x/y/issues/1",
                    "",
                    "accepted",
                    "not_published",
                    "zzz",
                    json.dumps({"body": "Done <!-- robert-comment task_id=task-1 attempt_id=attempt-1 action_index=0 -->"}),
                ),
            )
        self.cycle()
        self.assertEqual(self.cycle(600)["pending_count"], 0)


class RecoveryBudgetTests(unittest.TestCase):
    def test_authentication_failure_never_consumes_resume_quota(self):
        from tests.test_worker_lifecycle_workflow import WorkerLifecycleWorkflowTests

        fixture = WorkerLifecycleWorkflowTests()
        self.addCleanup(fixture.doCleanups)
        db = fixture._database()
        evidence = {
            "identity_status": "dead",
            "status": "recoverable_progress",
            "task_id": "task:test",
            "previous_attempt_id": "attempt:test",
            "progress_signals": ["worker_stderr_tail_available"],
            "logs": {"worker_stderr": {"tail": "401 Unauthorized"}},
        }
        with closing(sqlite3.connect(db)) as c, c:
            self.assertEqual(
                execution_operations.classify_recovery(
                    c,
                    "task",
                    "task:test",
                    "attempt:test",
                    evidence,
                    now="2026-10-23T00:00:00+00:00",
                ),
                "attention",
            )
            self.assertEqual(
                execution_store.get_binding(c, "task", "task:test")["recovery_attempts"],
                0,
            )


class DurableResultTests(unittest.TestCase):
    def setUp(self):
        from tests.test_worker_protocol import WorkerProtocolTests

        self.fixture = WorkerProtocolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture._init_attempt_db(output_type="local_result", route_id="local-result")
        from robert_agent.worker.result import build_result

        self.payload = build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="finished work",
            used_skills=[],
        )

    def test_locked_final_result_replays_once_without_rerunning_worker(self):
        from robert_agent.worker import result

        with closing(sqlite3.connect(self.db)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            response = result.record_result(self.db, self.payload)
            self.assertTrue(response["ok"])
            self.assertEqual(response["status"], "deferred")
            blocker.rollback()
        result.replay_pending_results(self.db)
        result.replay_pending_results(self.db)
        duplicate = result.record_result(self.db, self.payload)
        self.assertTrue(duplicate["ok"])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 1
            )
            self.assertEqual(c.execute("SELECT count(*) FROM wakeups").fetchone()[0], 1)

    def test_canceled_attempt_cannot_be_revived_by_result_replay(self):
        from robert_agent.worker import result

        with mock.patch.object(
            result,
            "_record_result_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result.record_result(self.db, self.payload)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE attempts SET status='canceled'")
            c.execute("UPDATE tasks SET lifecycle='canceled'")
        replay = result.replay_pending_results(self.db)
        self.assertEqual(replay["rejected"], 1)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0
            )


class DeliveryBudgetTests(unittest.TestCase):

    def test_operator_delivery_is_persisted_and_not_repeated(self):
        from tests.test_worker_protocol import WorkerProtocolTests
        from robert_agent import notify

        fixture = WorkerProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture._init_attempt_db()
        notify.record_notification(
            db,
            "worker_failed",
            "recorded",
            {"task_id": "task-1", "reason_code": "authentication_required"},
        )
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0))
        notify.deliver_pending(db, command=["test-notifier"], runner=runner)
        notify.deliver_pending(db, command=["test-notifier"], runner=runner)
        self.assertEqual(runner.call_count, 1)
        with closing(sqlite3.connect(db)) as c:
            metadata = json.loads(
                c.execute("SELECT metadata_json FROM notifications").fetchone()[0]
            )
            self.assertEqual(metadata["delivery"]["status"], "sent")


class ObservabilityTests(unittest.TestCase):
    def test_codex_turn_usage_is_counted(self):
        from robert_agent import usage

        parsed = usage.parse_cbc_stream_json(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            )
        )
        self.assertTrue(parsed["usage_available"])
        self.assertEqual(parsed["usage"]["input_tokens"], 100)

    def test_phase_records_actual_finish_instead_of_cycle_time(self):
        from robert_agent import run_once

        with closing(sqlite3.connect(":memory:")) as c:
            c.execute(
                "CREATE TABLE run_steps(step_id TEXT,run_id TEXT,step_key TEXT,status TEXT,started_at TEXT,finished_at TEXT,output_json TEXT,error_json TEXT)"
            )
            run_once._insert_run_step(c, "r", "publish")
            with (
                mock.patch.object(
                    run_once,
                    "_now",
                    side_effect=[
                        "2026-10-23T00:00:00+00:00",
                        "2026-10-23T00:01:00+00:00",
                    ],
                ),
                mock.patch.object(run_once.time, "monotonic", side_effect=[100, 160]),
            ):
                run_once._mark_run_step(
                    c, "r", "publish", "running", "2026-10-23T00:00:00+00:00"
                )
                run_once._mark_run_step(
                    c, "r", "publish", "succeeded", "2026-10-23T00:00:00+00:00"
                )
            row = c.execute("SELECT started_at,finished_at FROM run_steps").fetchone()
            self.assertNotEqual(row[0], row[1])
            self.assertEqual(
                json.loads(
                    c.execute("SELECT output_json FROM run_steps").fetchone()[0]
                )["duration_seconds"],
                60,
            )




class WaitingResultTests(unittest.TestCase):
    setUp = DurableResultTests.setUp

    def test_github_question_without_public_action_is_rejected_before_completion(self):
        from robert_agent.worker import result

        self.payload["output_type"] = "waiting_for_user"
        self.payload["submission"].update(
            operator_question={
                "kind": "clarification",
                "summary": "Which version?",
                "choices": [],
            },
        )
        response = result.record_result(self.db, self.payload)
        self.assertEqual(response["status"], "failed_validation")
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT lifecycle FROM tasks").fetchone()[0], "running")
            self.assertEqual(c.execute("SELECT status FROM attempts").fetchone()[0], "running")
            self.assertIsNone(c.execute("SELECT failure_json FROM attempts").fetchone()[0])
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0
            )
            self.assertEqual(
                c.execute("SELECT count(*) FROM notifications").fetchone()[0], 0
            )
        self.payload["output_type"] = "local_result"
        self.payload["submission"].pop("operator_question")
        self.assertTrue(result.record_result(self.db, self.payload)["ok"])


class TransactionBoundaryTests(unittest.TestCase):
    def test_worktree_preparation_does_not_hold_domain_write_lock(self):
        from robert_agent import run_once
        from tests.test_run_once import RunOnceDryRunTests

        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = run_once._prepare_worktree
        checked = []

        def prepare(*args, **kwargs):
            with closing(
                sqlite3.connect(fixture.data_dir / "dd.sqlite3", timeout=0.01)
            ) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            checked.append(True)
            return original(*args, **kwargs)

        with mock.patch.object(run_once, "_prepare_worktree", side_effect=prepare):
            result = run_once.run_once(
                fixture.config_path,
                fixture_path=fixture.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(checked)


class RecoveryEntryTests(unittest.TestCase):
    def test_operator_can_start_new_subject_after_attention_and_dead_worker(self):
        from tests.test_work_items import WorkItemCommandTests
        from robert_agent import work_items, run_once

        fixture = WorkItemCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task_id = fixture._failed_github_task()
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET lifecycle='running' WHERE task_id=?", (task_id,)
            )
            execution_store.ensure_subject(conn, 'task', task_id, 'human_request', '2026-10-24T00:00:00+00:00')
            execution_store.mark_binding_attention(
                conn, "task", task_id, "2026-10-24T00:00:00+00:00"
            )
            conn.execute(
                "UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task_id,)
            )
        response = work_items.retrigger_failed_github_task(
            fixture.db_path,
            task_id,
            context=fixture.context,
            idempotency_key="recover-auth",
        )
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            self.assertEqual(
                execution_store.get_binding(conn, "task", task_id)["binding_status"],
                "terminal",
            )
            self.assertTrue(
                run_once._execution_parent_allows_child(
                    conn, task_id, retrigger_wakeup_id=response["wakeup_id"]
                )
            )


class SpoolValidationTests(unittest.TestCase):
    def test_malformed_shape_is_quarantined_without_blocking_replay(self):
        from robert_agent import result_spool

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db"
            storage.init_database(db)
            root = result_spool.directory(db)
            root.mkdir()
            (root / "bad.json").write_text("[]")
            result = result_spool.replay(db, mock.Mock())
            self.assertEqual(result["rejected"], 1)
            self.assertTrue((root / "bad.invalid").exists())


class HealthTests(unittest.TestCase):
    setUp = PublicationBudgetTests.setUp

    def test_health_reports_delayed_publication_and_storage_without_deleting(self):
        from robert_agent import reliability_health

        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(
                "UPDATE github_actions SET created_at='2020-01-01T00:00:00+00:00'"
            )
        first = reliability_health.collect(
            self.db, now=self.now.isoformat(), repo_ids=["repo"]
        )
        reliability_health.collect(self.db, now=self.now.isoformat(), repo_ids=["repo"])
        self.assertEqual(first["delayed_tasks_sample"], 1)
        self.assertGreater(first["database_bytes"], 0)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM notifications").fetchone()[0], 1
            )
            self.assertEqual(c.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)






class ReplayBudgetTests(unittest.TestCase):
    setUp = DurableResultTests.setUp

    def test_busy_replay_stops_after_first_short_wait(self):
        import time
        from robert_agent import result_spool, result_contract
        from robert_agent.worker.result import replay_pending_results

        for number in range(2):
            envelope = {**self.payload, "attempt_id": f"pending-{number}"}
            digest = result_contract.semantic_fingerprint(envelope)
            payload = {key: value for key, value in envelope.items() if key != "submission"} | envelope["submission"]
            payload.update(_result_contract=result_contract.CONTRACT_ID, _normalizer_version=1, _submission_fingerprint=digest)
            with result_spool.locked_record(self.db, payload["attempt_id"]) as path:
                result_spool.atomic_write(
                    path,
                    {
                        "status": "pending",
                        "payload": payload,
                        "fingerprint": digest,
                        "result_contract": result_contract.CONTRACT_ID, "normalizer_version": 1,
                    },
                )
        with closing(sqlite3.connect(self.db)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = replay_pending_results(self.db)
            elapsed = time.monotonic() - started
            lock.rollback()
        self.assertLess(elapsed, 1.5)
        self.assertEqual(result["deferred"], 1)

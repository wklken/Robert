"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import (
    storage,
    publish,
    loop_engine,
    workflow_operations,
    workflow_store,
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
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a','t',1,'completed')"
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
                (json.dumps({"audit": {"status": "accepted", "action_scope": scope}}),),
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
                    json.dumps({"body": "Done <!-- robert-comment r -->"}),
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
                workflow_operations.classify_recovery(
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
                workflow_store.get_binding(c, "task", "task:test")["recovery_attempts"],
                0,
            )


class DurableResultTests(unittest.TestCase):
    def setUp(self):
        from tests.test_worker_protocol import WorkerProtocolTests

        self.fixture = WorkerProtocolTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture._init_attempt_db()
        from robert_agent.worker.result import build_result

        self.payload = build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
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
    def test_outbox_exhaustion_stops_delivery_and_exposes_attention(self):
        from tests.test_workflow_signals import WorkflowSignalTests, NOW
        from robert_agent import workflow_signals

        fixture = WorkflowSignalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        binding = fixture._binding()
        with closing(sqlite3.connect(fixture.db_path)) as c, c:
            signal = workflow_signals.record_subject_signal(
                c, binding, "worker_result", "r", {"fact_id": "r"}, NOW
            )
            for i in range(3):
                workflow_store.mark_signal_retry(
                    c,
                    signal["signal_id"],
                    f"2026-08-02T00:0{i}:00+00:00",
                    "ConnectionError",
                )
            self.assertEqual(
                c.execute("SELECT status FROM workflow_outbox").fetchone()[0],
                "attention",
            )
            self.assertGreater(
                c.execute("SELECT count(*) FROM notifications").fetchone()[0], 0
            )

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


class ProductionWorkflowTests(unittest.TestCase):
    setUp = PublicationBudgetTests.setUp

    def test_production_driver_publishes_and_finalizes_owned_task(self):
        from robert_agent.workflow_driver import ProductionDriver

        now = self.now.isoformat()
        with closing(sqlite3.connect(self.db)) as c, c:
            workflow_store.bind_subject(
                c, "task", "t", "human_request", "dbos", now, definition_version="v2"
            )
            c.execute("UPDATE workstreams SET active_task_id='t'")
        config = {
            "db_path": str(self.db),
            "data_dir": self.tmp.name,
            "max_concurrency": 1,
            "python_bin": sys.executable,
            "repos": [
                {
                    "full_name": "x/y",
                    "repo_root": self.tmp.name,
                    "worktree_root": self.tmp.name,
                }
            ],
            "workflow_engine": {"mode": "active"},
        }
        runner = mock.Mock(
            return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        driver = ProductionDriver(config, publish_runner=runner)
        response = driver.advance("task", "t")
        self.assertEqual(response["status"], "completed")
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT lifecycle FROM tasks").fetchone()[0], "completed"
            )
            self.assertEqual(
                c.execute("SELECT publish_status FROM github_actions").fetchone()[0],
                "published",
            )
            self.assertEqual(
                c.execute("SELECT binding_status FROM workflow_bindings").fetchone()[0],
                "terminal",
            )
        driver.advance("task", "t")
        self.assertEqual(runner.call_count, 1)

    def test_v2_binding_does_not_change_v1_workflow_identity(self):
        from robert_agent import dbos_runtime

        now = self.now.isoformat()
        with closing(sqlite3.connect(self.db)) as c, c:
            binding = workflow_store.bind_subject(
                c, "task", "t", "human_request", "dbos", now, definition_version="v2"
            )
        workflow_id, subject_id, function = dbos_runtime._workflow_for_binding(binding)
        self.assertEqual(workflow_id, "robert:v2:task:t")
        self.assertEqual(subject_id, "t")
        self.assertEqual(function.__name__, "human_request_workflow_v2")


class WaitingResultTests(unittest.TestCase):
    setUp = DurableResultTests.setUp

    def test_github_question_without_public_action_is_rejected_before_completion(self):
        from robert_agent.worker import result

        self.payload.update(
            output_type="waiting_for_user",
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
            failure = json.loads(c.execute("SELECT failure_json FROM attempts").fetchone()[0])
            self.assertEqual(failure["reason_code"], "question_not_deliverable")
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0
            )
            self.assertEqual(
                c.execute("SELECT count(*) FROM notifications").fetchone()[0], 1
            )


class ActiveEntryTests(unittest.TestCase):
    def test_new_active_task_is_dbos_owned_before_any_legacy_dispatch(self):
        import os
        from robert_agent import run_once
        from tests.test_run_once import RunOnceDryRunTests

        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with fixture.config_path.open("a") as f:
            f.write(
                "\nworkflow_engine:\n  mode: active\n  system_database_url_env: ROBERT_TEST_SYSTEM_DB\n"
            )
        with mock.patch.dict(
            os.environ, {"ROBERT_TEST_SYSTEM_DB": "postgresql://localhost/test"}
        ):
            response = run_once.run_once(
                fixture.config_path,
                fixture_path=fixture.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        self.assertTrue(response["ok"], response)
        with closing(sqlite3.connect(fixture.data_dir / "dd.sqlite3")) as c:
            binding = c.execute(
                "SELECT owner_engine,definition_version FROM workflow_bindings"
            ).fetchone()
            self.assertEqual(binding, ("dbos", "v2"))
            self.assertEqual(
                c.execute("SELECT status FROM attempts").fetchone()[0], "prepared"
            )

    def test_real_dbos_runtime_runs_production_driver_with_idempotent_start(self):
        import os
        import time
        from robert_agent import dbos_runtime, workflow_driver

        fixture = PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        dbos_runtime.destroy_runtime()
        self.addCleanup(dbos_runtime.destroy_runtime)
        with closing(sqlite3.connect(fixture.db)) as c, c:
            workflow_store.bind_subject(
                c,
                "task",
                "t",
                "human_request",
                "dbos",
                fixture.now.isoformat(),
                definition_version="v2",
            )
            c.execute("UPDATE workstreams SET active_task_id='t'")
        config = {
            "db_path": str(fixture.db),
            "data_dir": fixture.tmp.name,
            "max_concurrency": 1,
            "python_bin": sys.executable,
            "repos": [
                {
                    "full_name": "x/y",
                    "repo_root": fixture.tmp.name,
                    "worktree_root": fixture.tmp.name,
                }
            ],
            "workflow_engine": {
                "mode": "active",
                "system_database_url_env": "ROBERT_TEST_SYSTEM_DB",
            },
        }
        runner = mock.Mock(
            return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        )
        workflow_driver.configure_driver(config).publish_runner = runner
        with mock.patch.dict(
            os.environ,
            {
                "ROBERT_TEST_SYSTEM_DB": "sqlite:///"
                + str(Path(fixture.tmp.name) / "system.db")
            },
        ):
            runtime = dbos_runtime.configure_runtime(config)
            dbos_runtime.launch_runtime(runtime)
            driver = workflow_driver.current_driver()
            driver.start_pending()
            driver.start_pending()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                statuses = runtime.dbos.list_workflows(
                    workflow_ids=["robert:v2:task:t"]
                )
                if statuses and statuses[0].status == "SUCCESS":
                    break
                time.sleep(0.05)
            self.assertEqual(statuses[0].status, "SUCCESS")
            self.assertEqual(
                runtime.dbos.retrieve_workflow("robert:v2:task:t").get_result()[
                    "status"
                ],
                "completed",
            )
        self.assertEqual(runner.call_count, 1)


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
            workflow_store.bind_subject(
                conn,
                "task",
                task_id,
                "human_request",
                "dbos",
                "2026-10-24T00:00:00+00:00",
                definition_version="v2",
            )
            workflow_store.mark_binding_attention(
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
                workflow_store.get_binding(conn, "task", task_id)["binding_status"],
                "terminal",
            )
            self.assertTrue(
                run_once._legacy_parent_allows_child(
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


class DriverCrashTests(unittest.TestCase):
    def setUp(self):
        import os
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import run_once, validate_config

        self.fixture = RunOnceDryRunTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with self.fixture.config_path.open("a") as f:
            f.write(
                "\nworkflow_engine:\n  mode: active\n  system_database_url_env: ROBERT_TEST_SYSTEM_DB\n"
            )
        with mock.patch.dict(
            os.environ, {"ROBERT_TEST_SYSTEM_DB": "postgresql://localhost/test"}
        ):
            result = run_once.run_once(
                self.fixture.config_path,
                fixture_path=self.fixture.fixture_path,
                dry_run=True,
                skip_external=True,
            )
            self.config = validate_config.validate_config(
                self.fixture.config_path, skip_external=True
            )
        self.assertTrue(result["ok"], result)
        self.db = self.fixture.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(self.db)) as c:
            self.task_id, self.attempt_id = c.execute(
                "SELECT task_id,attempt_id FROM attempts"
            ).fetchone()

    def test_dispatch_ambiguous_crash_never_spawns_twice(self):
        from robert_agent import workflow_driver, run_once

        driver = workflow_driver.ProductionDriver(self.config)
        # Simulates process death after dispatch fence, before PID persistence.
        with mock.patch.object(
            run_once, "_dispatch_queued", side_effect=SystemExit("crash")
        ) as dispatch:
            with self.assertRaises(SystemExit):
                driver.advance("task", self.task_id)
            self.assertEqual(dispatch.call_count, 1)
        with mock.patch.object(run_once, "_dispatch_queued") as dispatch:
            outcome = driver.advance("task", self.task_id)
            self.assertEqual(outcome["reason_code"], "dispatch_outcome_unknown")
            dispatch.assert_not_called()

    def test_canceled_binding_never_dispatches(self):
        from robert_agent import workflow_driver, run_once

        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE tasks SET lifecycle='canceled'")
            workflow_store.close_binding(
                c, "task", self.task_id, "2026-10-24T00:00:00+00:00"
            )
        with mock.patch.object(run_once, "_dispatch_queued") as dispatch:
            workflow_driver.ProductionDriver(self.config).advance("task", self.task_id)
            dispatch.assert_not_called()


class ReviewRegressionTests(unittest.TestCase):
    setUp = DriverCrashTests.setUp

    def test_reply_batch_is_transferred_before_parent_closes(self):
        from robert_agent import run_once, workflow_signals, workflow_driver

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='waiting_for_user'")
            conn.execute("UPDATE attempts SET status='completed'")
            conn.execute("UPDATE workstreams SET lifecycle='waiting_for_user'")
            event = json.loads(
                conn.execute("SELECT payload_json FROM github_events").fetchone()[0]
            )
            binding = workflow_store.get_binding(conn, "task", self.task_id)
            for number in range(3):
                reply = {
                    **event,
                    "event_fingerprint": f"reply-{number}",
                    "event_type": "comment",
                    "actor_login": self.config["repos"][0]["trusted_actors"][0],
                    "authorization_status": "accepted_context",
                    "drives_execution": number != 1,
                    "body": f"Reply {number}",
                }
                run_once._insert_source_and_event(
                    conn, "repo:example/backend", reply, "2026-10-24T00:00:00+00:00"
                )
                workflow_signals.record_subject_signal(
                    conn,
                    binding,
                    "subject-events",
                    reply["event_fingerprint"],
                    {"fact_id": reply["event_fingerprint"]},
                    "2026-10-24T00:00:00+00:00",
                )
        original = run_once._prepare_worktree
        with (
            mock.patch.object(
                run_once,
                "_prepare_worktree",
                side_effect=lambda repo, event, route, dry: original(
                    repo, event, route, True
                ),
            ),
            mock.patch.object(run_once, "_dispatch_queued"),
        ):
            workflow_driver.ProductionDriver(self.config).advance("task", self.task_id)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM workflow_signal_receipts"
                ).fetchone()[0],
                3,
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM task_events te JOIN github_events ge ON ge.event_id=te.event_id WHERE te.task_id != ? AND ge.event_fingerprint LIKE 'reply-%'",
                    (self.task_id,),
                ).fetchone()[0],
                3,
            )

    def test_fresh_timeout_retains_binding_for_next_cycle_recovery(self):
        from robert_agent import run_once, workflow_driver

        now = "2026-10-24T00:00:00+00:00"

        def timeout(conn, *_args, **_kwargs):
            conn.execute(
                "UPDATE attempts SET status='failed',finished_at=?,failure_json=?",
                (now, json.dumps({"status": "failed_timeout"})),
            )
            conn.execute("UPDATE tasks SET lifecycle='failed'")
            conn.execute(
                "UPDATE workstreams SET lifecycle='failed',active_task_id=NULL"
            )

        driver = workflow_driver.ProductionDriver(self.config)
        with (
            mock.patch.object(run_once, "_now", return_value=now),
            mock.patch.object(
                run_once, "_supervise_running_attempts", side_effect=timeout
            ),
        ):
            outcome = driver.advance("task", self.task_id)
        self.assertEqual(outcome["status"], "waiting")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                workflow_store.get_binding(conn, "task", self.task_id)[
                    "binding_status"
                ],
                "active",
            )
        evidence = {
            "status": "recoverable_progress",
            "task_id": self.task_id,
            "previous_attempt_id": self.attempt_id,
            "progress_signals": ["dirty_worktree"],
        }
        with (
            mock.patch.object(
                run_once, "_now", return_value="2026-10-24T00:00:01+00:00"
            ),
            mock.patch.object(
                run_once, "_dispatch_process_status", return_value={"status": "dead"}
            ),
            mock.patch.object(
                run_once,
                "_inspect_attempt_recovery_with_metadata",
                return_value=evidence,
            ),
            mock.patch.object(run_once, "_dispatch_queued"),
        ):
            self.assertEqual(driver.advance("task", self.task_id)["status"], "waiting")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2
            )
            self.assertEqual(
                workflow_store.get_binding(conn, "task", self.task_id)[
                    "recovery_attempts"
                ],
                1,
            )
            latest = conn.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_no DESC LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                "UPDATE attempts SET status='failed',finished_at=?,failure_json=? WHERE attempt_id=?",
                (now, json.dumps({"status": "failed_timeout"}), latest),
            )
            conn.execute("UPDATE tasks SET lifecycle='failed'")
            conn.execute(
                "UPDATE workstreams SET lifecycle='failed',active_task_id=NULL"
            )
        evidence["previous_attempt_id"] = latest
        with (
            mock.patch.object(
                run_once, "_now", return_value="2026-10-24T00:00:02+00:00"
            ),
            mock.patch.object(
                run_once, "_dispatch_process_status", return_value={"status": "dead"}
            ),
            mock.patch.object(
                run_once,
                "_inspect_attempt_recovery_with_metadata",
                return_value=evidence,
            ),
        ):
            self.assertEqual(
                driver.advance("task", self.task_id)["status"], "attention"
            )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2
            )


class ReplayBudgetTests(unittest.TestCase):
    setUp = DurableResultTests.setUp

    def test_busy_replay_stops_after_first_short_wait(self):
        import time
        from robert_agent import result_spool
        from robert_agent.worker.result import replay_pending_results

        for number in range(2):
            payload = {**self.payload, "attempt_id": f"pending-{number}"}
            with result_spool.locked_record(self.db, payload["attempt_id"]) as path:
                result_spool.atomic_write(
                    path,
                    {
                        "status": "pending",
                        "payload": payload,
                        "fingerprint": result_spool.fingerprint(payload),
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


class RemediationDeliveryTests(unittest.TestCase):
    def test_failed_dbos_episode_receives_and_consumes_new_budgeted_decision(self):
        from tests.test_pr_remediation_integration import PrRemediationIntegrationTests
        from robert_agent import (
            run_once,
            route,
            workstream,
            workflow_ownership,
            workflow_driver,
        )

        fixture = PrRemediationIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            token = workflow_ownership.creation_mode.set("active")
            try:
                event = run_once._collect_pr_remediation_decisions(
                    conn,
                    "repo:example/backend",
                    fixture.repo,
                    fixture.runner(),
                    "2026-07-26T00:01:00+00:00",
                )[0]
                episode_id = event["metadata"]["remediation"]["episode_id"]
                task = run_once._create_task_attempt_and_prompt(
                    conn,
                    Path(fixture.tmp.name),
                    fixture.db_path,
                    fixture.repo,
                    "repo:example/backend",
                    event,
                    route.route_task(event),
                    workstream.plan_event(event, active_workstreams=set()),
                    "2026-07-26T00:01:00+00:00",
                    worktree_result=run_once._prepare_worktree(
                        fixture.repo, event, route.route_task(event), True
                    ),
                )
            finally:
                workflow_ownership.creation_mode.reset(token)
            with workflow_ownership.executing("remediation_episode", episode_id):
                run_once._finalize_failed_task(
                    conn,
                    task["task_id"],
                    task["workstream_id"],
                    "2026-07-26T00:02:00+00:00",
                    {"status": "worker_failed"},
                )
            conn.execute("UPDATE attempts SET status='failed'")
            run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                fixture.repo,
                fixture.runner(),
                "2026-07-26T00:03:00+00:00",
            )
            run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                fixture.repo,
                fixture.runner(),
                "2026-07-26T00:03:00+00:00",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM workflow_outbox WHERE topic='remediation_decision'"
                ).fetchone()[0],
                1,
            )
        skill_root = Path(fixture.tmp.name) / "skills"
        for name in ("fast-conflict-fix", "fast-preflight"):
            path = skill_root / name
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nFixture skill")
        config = {
            "db_path": str(fixture.db_path),
            "data_dir": fixture.tmp.name,
            "python_bin": sys.executable,
            "repos": [fixture.repo],
            "workers": [{"name": "default"}],
            "skills": {"search_paths": [str(skill_root)]},
        }
        original = run_once._prepare_worktree
        with (
            mock.patch.object(
                run_once, "_now", return_value="2026-07-26T00:04:00+00:00"
            ),
            mock.patch.object(
                run_once,
                "_prepare_worktree",
                side_effect=lambda repo, event, route, dry: original(
                    repo, event, route, True
                ),
            ),
            mock.patch.object(run_once, "_dispatch_queued"),
        ):
            response = workflow_driver.ProductionDriver(config).advance(
                "remediation_episode", episode_id
            )
        self.assertEqual(response["status"], "waiting", response)
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute(
                    "SELECT attempt_count FROM pr_remediation_episodes WHERE episode_id=?",
                    (episode_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM workflow_signal_receipts"
                ).fetchone()[0],
                1,
            )

from contextlib import closing
from datetime import datetime, timezone
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.support import pr_remediation_integration as health_fixture
from tests.support import durable_result as result_fixture
from tests.support import review_coordinator as review_fixture
from tests.support import repository_cycle as run_fixture
from robert_agent.adapters.processes import worker as dispatch
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.adapters.sqlite import review_rounds as review_store
from robert_agent.runtime import cycle as run_once
from robert_agent.adapters.processes import identity as worker_identity
from robert_agent.delivery.application import submission as result
from robert_agent.execution.application import dispatch as dispatch_queue
from robert_agent.execution.application import evidence
from robert_agent.execution.application import monitor
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import completion
from robert_agent.work.application import remediation_requests
from robert_agent.work.application import source_lifecycle
from robert_agent.execution import capacity
from robert_agent.foundation import values
from robert_agent.work.application import preparation
from tests.support.lifecycle_remediation import LifecycleRemediationFixture


class LifecycleRemediationTests(LifecycleRemediationFixture):
    def test_classification_handoff_survives_git_failure_with_bounded_retry(self):
        fixture = run_fixture.RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        event = {
            "id": "assign-handoff",
            "number": 16,
            "source_type": "issue",
            "event_type": "assigned",
            "actor_login": "robert-bot",
            "assignment_actor_login": "wklken",
            "assigned_to": "robert-bot",
            "authorization_lookup_complete": True,
            "event_fingerprint": "assigned:assign-handoff",
            "body": "remove the DCO check",
            "intent": "unclear",
            "title": "CI change",
            "state": "open",
        }
        fixture.fixture_path.write_text(json.dumps({"events": [event]}))
        self.assertTrue(
            run_once.run_once(
                fixture.config_path,
                fixture_path=fixture.fixture_path,
                dry_run=True,
                skip_external=True,
            )["ok"]
        )
        db = fixture.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            task, attempt = conn.execute(
                "SELECT task_id,attempt_id FROM attempts"
            ).fetchone()
        payload = {
            "task_id": task,
            "attempt_id": attempt,
            "output_type": "classification_result",
            "submission": {
                "planned_github_actions": [],
                "consumed_event_fingerprints": ["assigned:assign-handoff"],
                "verification": [],
                "handoff": "Route to implementation",
                "used_skills": [],
                "recommended_route": "new-pr",
                "branch_slug": "ci-change",
            },
        }
        self.assertTrue(result.record_result(db, payload)["ok"])
        for failure in range(1, 4):
            with mock.patch.object(
                assigned_workspace,
                "prepare_worktree",
                side_effect=subprocess.CalledProcessError(
                    1, ["git", "fetch"], stderr="connection reset by peer"
                ),
            ):
                run_once.run_once(fixture.config_path, dry_run=True, skip_external=True)
            with closing(sqlite3.connect(db)) as conn:
                metadata = json.loads(
                    conn.execute(
                        "SELECT metadata_json FROM tasks WHERE task_id=?", (task,)
                    ).fetchone()[0]
                )
                handoff = metadata.get("classification_handoff", {})
                self.assertEqual(handoff.get("attempt_count"), failure)
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM tasks WHERE parent_task_id=?", (task,)
                    ).fetchone()[0],
                    0,
                )
                if failure < 3:
                    due = handoff["next_retry_at"]
                    self.assertFalse(
                        completion.has_orphaned_classification_result(
                            conn, ["repo:example/backend"], handoff["failed_at"]
                        )
                    )
                    self.assertTrue(
                        completion.has_orphaned_classification_result(
                            conn, ["repo:example/backend"], due
                        )
                    )
                else:
                    self.assertFalse(handoff["retryable"])
                    self.assertFalse(
                        completion.has_orphaned_classification_result(
                            conn, ["repo:example/backend"], "2070-01-01T00:00:00+00:00"
                        )
                    )
            if failure < 3:
                # Advance the caller's clock to this durable retry deadline.
                clock = mock.patch.object(values, "utc_now", return_value=due)
                clock.start()
                self.addCleanup(clock.stop)

    def test_stale_live_worker_keeps_dispatch_capacity(self):
        fixture, process, repo, now = self.running_fixture(age_minutes=25)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            monitor.supervise_running_attempts(
                conn,
                repo,
                {},
                now,
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
            self.assertIsNone(process.poll())
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "stale"
            )
            self.assertEqual(capacity.running_attempt_count(conn, repo), 1)
            self.assertEqual(capacity.running_attempt_count_for_repos(conn, [repo]), 1)

    def test_received_result_retains_ownership_until_process_exit(self):
        fixture, process, repo, now = self.running_fixture()
        self.assertTrue(result.record_result(fixture.db, fixture.payload)["ok"])
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            self.assertEqual(capacity.running_attempt_count(conn, repo), 1)
            self.assertNotEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "completed"
            )
        self.stop_process(process)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            monitor.supervise_running_attempts(
                conn,
                repo,
                {},
                now,
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "completed"
            )
            self.assertEqual(capacity.running_attempt_count(conn, repo), 0)

    def test_result_arriving_during_recovery_observation_is_not_failed(self):
        fixture, process, repo, now = self.running_fixture()
        self.stop_process(process)
        receipts = []

        def inspect(*args, **kwargs):
            receipts.append(result.record_result(fixture.db, fixture.payload))
            return {"status": "no_progress", "commands": [], "progress_signals": []}

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(
                evidence, "inspect_attempt_recovery_with_metadata", side_effect=inspect
            ):
                monitor.supervise_running_attempts(
                    conn,
                    repo,
                    {},
                    now,
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            self.assertTrue(receipts[0]["ok"])
            self.assertEqual(receipts[0]["status"], "recorded")
            self.assertNotEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "failed"
            )
            monitor.supervise_running_attempts(
                conn,
                repo,
                {},
                now,
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "completed"
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1
            )

    def test_result_arriving_during_timeout_cleanup_is_not_failed(self):
        fixture, process, repo, now = self.running_fixture(age_minutes=50)
        receipts = []

        def terminate(*args, **kwargs):
            args[0].commit()  # Match the real cleanup helper's unlocked I/O boundary.
            self.stop_process(process)
            # Model a receipt that already passed submission serialization before
            # cleanup began; the supervisor must recheck the committed DB fact.
            from robert_agent.delivery import contract as result_contract

            payload, rejection = result._prepare(fixture.db, fixture.payload)
            self.assertIsNone(rejection)
            payload.update(
                _submission_fingerprint=result_contract.semantic_fingerprint(
                    fixture.payload
                ),
                _result_contract=result_contract.CONTRACT_ID,
                _normalizer_version=result_contract.NORMALIZER_VERSION,
            )
            receipts.append(result._record_result_db(fixture.db, payload))
            return {"status": "already_dead"}

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(
                monitor, "terminate_owned_attempt", side_effect=terminate
            ):
                monitor.supervise_running_attempts(
                    conn,
                    repo,
                    {},
                    now,
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            self.assertTrue(receipts[0]["ok"])
            self.assertEqual(receipts[0]["status"], "recorded")
            self.assertNotEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "failed"
            )
            monitor.supervise_running_attempts(
                conn,
                repo,
                {},
                now,
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "completed"
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1
            )

    def test_recovery_backfill_does_not_hold_writer_during_git_inspection(self):
        from pathlib import Path

        fixture, process, _repo, now = self.running_fixture()
        self.stop_process(process)
        directory = Path(fixture.db).parent / "artifacts" / "task-1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "attempt-1.verify.command.json").write_text(
            json.dumps(
                {
                    "task_id": "task-1",
                    "attempt_id": "attempt-1",
                    "phase": "verify",
                    "status": "completed",
                    "returncode": 0,
                    "finished_at": now,
                    "snapshot_status": "deferred",
                    "summary": "Command exited 0",
                }
            )
        )
        observed = []

        def inspect_git(_path):
            with closing(sqlite3.connect(fixture.db, timeout=0.05)) as other, other:
                other.execute("BEGIN IMMEDIATE")
                observed.append(True)
            return {}

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(
                evidence, "git_recovery_summary", side_effect=inspect_git
            ):
                recovery = evidence.inspect_attempt_recovery_with_metadata(
                    conn, "attempt-1", "task-1", {}, directory
                )
            self.assertEqual(recovery["status"], "recoverable_progress")
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM worker_phases WHERE summary='Command exited 0'"
                ).fetchone()[0],
                1,
            )
        self.assertEqual(observed, [True])

    def test_startup_command_inspection_releases_writer(self):
        fixture, _process, repo, now = self.running_fixture(age_minutes=10)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("DELETE FROM worker_phases")
        observed = []

        def inspect_commands(*args, **kwargs):
            with closing(sqlite3.connect(fixture.db, timeout=0.05)) as other, other:
                other.execute("BEGIN IMMEDIATE")
                observed.append(True)
            return [{"status": "running"}]

        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.object(
                evidence, "attempt_command_records", side_effect=inspect_commands
            ):
                monitor.supervise_running_attempts(
                    conn,
                    repo,
                    {},
                    now,
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "running"
            )
        self.assertEqual(observed, [True])

    def test_control_deadline_does_not_fail_recoverable_worker(self):
        from pathlib import Path

        fixture, process, repo, now = self.running_fixture()
        self.stop_process(process)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("DELETE FROM worker_phases")
            conn.execute(
                "UPDATE attempts SET worktree_path=?", (str(Path(fixture.db).parent),)
            )
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            with mock.patch.dict(os.environ, {"_ROBERT_CONTROL_DEADLINE": "0"}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    monitor.supervise_running_attempts(
                        conn,
                        repo,
                        {},
                        now,
                        resume_attempt=preparation.create_resume_attempt_and_prompt,
                    )
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "running"
            )
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "running"
            )
            self.assertEqual(
                conn.execute(
                    "SELECT binding_status FROM execution_subjects"
                ).fetchone()[0],
                "active",
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1
            )

    def test_fast_receipt_before_pid_registration_is_still_supervised(self):
        from robert_agent.runtime import loop as loop_engine

        fixture = result_fixture.DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='prepared',started_at=NULL,heartbeat_at=NULL,metadata_json=json_set('{}', '$.result_contract', 'compact-v1')"
            )
            task, repo = conn.execute("""SELECT t.task_id,w.repo_id FROM tasks t
                JOIN workstreams w ON w.workstream_id=t.workstream_id""").fetchone()
            execution_store.ensure_subject(
                conn,
                "task",
                task,
                "human_request",
                datetime.now(timezone.utc).isoformat(),
            )
        self.assertTrue(result.record_result(fixture.db, fixture.payload)["ok"])
        process, identity = self.process()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            dispatch_queue._record_dispatch_result(
                conn, "attempt-1", {**identity, "ok": True, "status": "running"}
            )
            self.assertEqual(capacity.running_attempt_count(conn, repo), 1)
        self.stop_process(process)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=[repo]))

    def test_health_reads_release_writer_between_prs(self):
        fixture = health_fixture.PrRemediationIntegrationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.repo["pr_automation"]["ci"]["enabled"] = False
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute("""INSERT INTO github_sources(source_id,repo_id,source_key,
                source_type,number,title,state,author_login) VALUES ('source:pr2',
                'repo:example/backend','github:example/backend!14','pull_request',
                14,'Fix2','open','robert-bot')""")
            conn.execute("""INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,
                origin_workstream_id,lifecycle,created_at,updated_at) VALUES (
                'github:example/backend!14','repo:example/backend','source:pr2',
                'github:example/backend#1','completed','2026-07-27','2026-07-27')""")
        delegate = fixture.runner()
        observed = []

        def runner(args, **kwargs):
            with closing(sqlite3.connect(fixture.db_path, timeout=0)) as other, other:
                other.execute("BEGIN IMMEDIATE")
            observed.append(args[-1])
            return delegate(
                [arg.replace("/pulls/14", "/pulls/13") for arg in args], **kwargs
            )

        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                fixture.repo,
                runner,
                "2026-10-26T00:00:00+00:00",
            )
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len(observed), 2)

    def test_remote_close_stops_registered_native_models(self):
        fixture = review_fixture.ReviewCoordinatorFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cycle([fixture.event()])
        driver = fixture.coordinator()
        driver.prepare()
        model, identity = self.process()
        dispatch._BACKGROUND_PROCESSES[model.pid] = model
        with review_store.connect(fixture.db) as conn:
            run = review_store.new_run(
                conn,
                driver.round_id,
                "review",
                "codex",
                driver.attempt_id,
                driver.manifest["artifact_root"],
                driver.manifest["prompt_sha256"],
            )
            review_store.update_run(
                conn,
                run["run_id"],
                status="running",
                slot_reserved=1,
                dispatch_json=json.dumps(identity),
            )
        ready = fixture.root / "coordinator-ready"
        program = """import signal,sys,time
from pathlib import Path
from robert_agent.workflows.review.coordinator import Coordinator
driver=Coordinator(sys.argv[1],sys.argv[2],sys.argv[3])
signal.signal(signal.SIGTERM,lambda *_:driver.stopping.set())
def prepare():
    Path(sys.argv[4]).touch()
    while True:
        driver.check_active()
        time.sleep(.05)
driver.prepare=prepare
driver.run()
"""
        coordinator = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                program,
                str(fixture.db),
                driver.task_id,
                driver.attempt_id,
                str(ready),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop_process, coordinator)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        top = worker_identity.capture_process_identity(coordinator.pid)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute(
                """UPDATE attempts SET status='running',started_at=?,
                heartbeat_at=?,metadata_json=? WHERE attempt_id=?""",
                (now, now, json.dumps({"dispatch": top}), driver.attempt_id),
            )
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            repo, stream, source, key, kind, number = conn.execute(
                """SELECT w.repo_id,
                w.workstream_id,gs.source_id,gs.source_key,gs.source_type,gs.number
                FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
                JOIN github_sources gs ON gs.source_id=w.primary_source_id
                WHERE t.task_id=?""",
                (driver.task_id,),
            ).fetchone()
            closed = source_lifecycle.record_remote_terminal_state(
                conn,
                repo,
                stream,
                source,
                key,
                kind,
                number,
                driver.task_id,
                {"state": "closed", "closed_at": now},
                "canceled",
                "remote_pr_closed",
                now,
            )
        self.assertTrue(closed)
        self.assertIsNotNone(
            model.poll(), "remote close must stop the independent model group"
        )
        with review_store.connect(fixture.db) as conn:
            self.assertEqual(review_store.process_pool_status(conn)["in_use"], 0)


if __name__ == "__main__":
    unittest.main()

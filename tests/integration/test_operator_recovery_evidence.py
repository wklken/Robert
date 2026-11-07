"""Acceptance gaps left by the first reliability delivery."""

from tests.support.durable_result import DurableResultFixture


from contextlib import closing

import sqlite3

import unittest

from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.execution.application import dispatch as dispatch_queue


from robert_agent.execution.application import monitor


from robert_agent.work.application import preparation


class ReviewRegressionTests(unittest.TestCase):
    def test_status_expands_home_for_all_database_reads(self):
        import os
        from robert_agent.readmodels import status

        fixture = DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.dict(os.environ, {"HOME": str(fixture.db.parent)}):
            self.assertIn("execution_timings", status.build_status("~/" + fixture.db.name))

    def test_prepared_and_failed_spawn_are_not_worker_execution(self):
        from robert_agent.readmodels import execution_metrics

        fixture = DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET created_at='2026-10-24T00:00:00Z'")
            conn.execute("UPDATE attempts SET status='prepared',started_at='2026-10-24T00:00:00Z'")
            self.assertIsNone(execution_metrics.task_timing(conn, "task-1")["queue_seconds"])
            dispatch_queue._record_dispatch_result(
                conn,
                "attempt-1",
                {
                    "ok": False,
                    "status": "failed_dispatch",
                    "safe_error": "cannot spawn",
                },
            )
            self.assertIsNone(execution_metrics.task_timing(conn, "task-1")["worker_seconds"])

    def test_terminal_tasks_hide_stale_wait_metadata(self):
        from tests.support.publication_budget import PublicationBudgetFixture
        from robert_agent.readmodels import execution_metrics

        fixture = PublicationBudgetFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET lifecycle='canceled', metadata_json=?",
                ('{"wait_state":{"reason_code":"repository_busy","since":"now","lease_expires_at":"later"}}',),
            )
            timing = execution_metrics.task_timing(conn, "t")
            self.assertEqual(timing["waiting_reason"], "terminal")
            self.assertIsNone(timing["waiting_since"])
            self.assertIsNone(timing["lease_expires_at"])

    def test_rejected_question_can_be_corrected_without_killing_worker(self):
        import json
        import subprocess
        import sys
        from datetime import datetime, timezone
        from tests.support.worker_protocol import WorkerProtocolFixture
        from robert_agent.adapters.processes import identity as worker_identity
        from robert_agent.delivery.application import submission as result

        fixture = WorkerProtocolFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture._init_attempt_db()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

        self.addCleanup(cleanup)
        identity = worker_identity.capture_process_identity(child.pid)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET started_at=?,heartbeat_at=?,metadata_json=json_set(metadata_json,'$.dispatch',json(?))",
                (now, now, json.dumps({"status": "running", **identity})),
            )
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="waiting_for_user",
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="question",
            operator_question={
                "kind": "clarification",
                "summary": "Which version?",
                "choices": [],
            },
        )
        rejected = result.record_result(db, payload)
        self.assertEqual(rejected["status"], "failed_validation")
        self.assertIsNone(child.poll())
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(
                conn.execute("SELECT status,failure_json FROM attempts").fetchone(),
                ("running", None),
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
        payload["submission"]["planned_github_actions"] = [
            {"type": "comment", "body": "Which version should I target?"}
        ]
        accepted = result.record_result(db, payload)
        self.assertTrue(accepted["ok"], accepted)
        self.assertIsNone(child.poll())
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")
        child.terminate()
        child.wait(timeout=10)
        with closing(sqlite3.connect(db)) as conn, conn:
            monitor.supervise_running_attempts(
                conn,
                "repo-1",
                {},
                now,
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "completed")

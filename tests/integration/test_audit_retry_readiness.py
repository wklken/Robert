"""Audit retries awaiting a model grant must not drive empty local cycles."""

from contextlib import closing
import json
import sqlite3
import unittest

from robert_agent.delivery.application import result_audit, submission
from robert_agent.execution.application import readiness
from robert_agent.foundation import values
from tests.support.compact_result import CompactResultFixture


class AuditRetryReadinessTests(unittest.TestCase):
    def test_audit_waiting_for_model_is_not_runnable(self):
        fixture = CompactResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db, task, attempt, _, _ = fixture.prepare("comment")
        recorded = submission.record_result(db, fixture.envelope(task, attempt))
        self.assertEqual(recorded["status"], "recorded")
        now = values.utc_now()
        config = {"supervision": {"mode": "decide"}}
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='completed' WHERE attempt_id=?", (attempt,))
            conn.execute("UPDATE tasks SET lifecycle='running' WHERE task_id=?", (task,))
            repo_id = conn.execute(
                "SELECT repo_id FROM workstreams WHERE workstream_id=(SELECT workstream_id FROM tasks WHERE task_id=?)",
                (task,),
            ).fetchone()[0]
            self.assertTrue(
                readiness.has_local_work(conn, [repo_id], config_result=config, now=now, include_publication=False)
            )
            raw = conn.execute(
                "SELECT metadata_json FROM worker_results WHERE result_id=?", (recorded["result_id"],)
            ).fetchone()[0]
            metadata = json.loads(raw)
            metadata["audit_observation"] = {"attempt_count": 1, "retryable": True}
            conn.execute(
                "UPDATE worker_results SET metadata_json=? WHERE result_id=?",
                (json.dumps(metadata), recorded["result_id"]),
            )
            conn.commit()
            self.assertFalse(
                readiness.has_local_work(conn, [repo_id], config_result=config, now=now, include_publication=False)
            )
            audited = result_audit.audit_completed_results(
                conn,
                repo_id,
                fixture.data_dir,
                db,
                {"full_name": "example/backend"},
                "audit-retry",
                now,
                False,
                [],
                {"remaining": 1},
                config_result=config,
            )
            self.assertEqual(audited, 0)
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id=?", (recorded["result_id"],)
                ).fetchone()[0]
            )
            self.assertNotIn("audit", metadata)

from contextlib import closing
import json
import sqlite3
from tests.support.worker_protocol import WorkerProtocolFixture


class SubmissionWakeupTests(WorkerProtocolFixture):
    def test_result_records_worker_result_ready_wakeup(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT repo_id, reason, dedupe_key, task_id, attempt_id,
                       result_id, status, metadata_json
                FROM wakeups
                """
            ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row[:7],
            (
                "repo-1",
                "worker_result_ready",
                record["result_id"],
                "task-1",
                "attempt-1",
                record["result_id"],
                "pending",
            ),
        )
        metadata = json.loads(row[7])
        self.assertEqual(metadata["recorded_by"], "robert.worker")
        self.assertEqual(metadata["output_type"], "comment_analysis")

    def test_bound_worker_result_records_native_wakeup(self):
        from robert_agent.persistence import execution_control as execution_store
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db(output_type="local_result", route_id="local-result")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            execution_store.ensure_subject(conn, "task", "task-1", "human_request", "2026-08-02T00:00:00+00:00")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM wakeups WHERE reason='worker_result_ready'").fetchone()[0],
                1,
            )

    def test_result_notifies_only_after_transaction_commits(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db(output_type="local_result", route_id="local-result")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )
        observed = []

        def committed_notifier(notified_db_path):
            with closing(sqlite3.connect(notified_db_path)) as conn:
                observed.append(
                    (
                        conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0],
                        conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0],
                        conn.execute("SELECT status FROM attempts WHERE attempt_id = 'attempt-1'").fetchone()[0],
                    )
                )

        record = result.record_result(
            db_path,
            payload,
            notifier=committed_notifier,
        )

        self.assertTrue(record["ok"], record)
        self.assertEqual(observed, [(1, 1, "completed")])

    def test_result_stays_successful_when_post_commit_notification_fails(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db(output_type="local_result", route_id="local-result")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        def failed_notifier(_db_path):
            raise OSError("listener unavailable")

        record = result.record_result(db_path, payload, notifier=failed_notifier)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0], 1)

    def test_duplicate_result_does_not_create_extra_wakeup(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        first = result.record_result(db_path, payload)
        duplicate = result.record_result(db_path, payload)
        self.assertTrue(duplicate["deduplicated"])

        self.assertTrue(first["ok"], first)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            wakeup_count = conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0]
        self.assertEqual(wakeup_count, 1)

    def test_rejected_result_does_not_record_wakeup(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status = 'failed' WHERE attempt_id = 'attempt-1'")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "rejected_by_supervisor")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            wakeup_count = conn.execute("SELECT COUNT(*) FROM wakeups").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
        self.assertEqual(wakeup_count, 0)
        self.assertEqual(result_count, 0)

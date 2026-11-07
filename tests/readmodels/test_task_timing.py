"""Acceptance gaps left by the first reliability delivery."""

from tests.support.durable_result import DurableResultFixture


from contextlib import closing

import sqlite3

import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


class TimingTests(unittest.TestCase):
    def test_timings_keep_queue_execution_and_ingestion_separate(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.readmodels import execution_metrics

        fixture = DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertTrue(result.record_result(fixture.db, fixture.payload)["ok"])
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET created_at='2026-10-24T00:00:00Z'")
            conn.execute(
                "UPDATE attempts SET started_at='2026-10-24T00:01:00Z', finished_at='2026-10-24T00:04:00Z',metadata_json=json_set(metadata_json,'$.worker_started_at','2026-10-24T00:01:00Z')"
            )
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(metadata_json, '$.result_saved_at','2026-10-24T00:03:00Z','$.result_ingested_at','2026-10-24T00:04:00Z')"
            )
            timing = execution_metrics.task_timing(conn, "task-1")
            self.assertEqual(timing["queue_seconds"], 60)
            self.assertEqual(timing["worker_seconds"], 120)
            self.assertEqual(timing["result_ingestion_seconds"], 60)
            self.assertIsNone(timing["result_to_publication_seconds"])
            report = execution_metrics.summarize(conn)
            group = next(iter(report["by_task_kind"].values()))
            self.assertEqual(group["queue_seconds"]["p95"], 60)

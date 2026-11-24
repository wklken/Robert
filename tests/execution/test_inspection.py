from datetime import datetime, timedelta, timezone
from tests.support.temp_workspace import TemporaryWorkspace


class InspectionTests(TemporaryWorkspace):
    def test_supervise_classifies_stale_and_timeout(self):
        from robert_agent.execution import inspection as supervise

        now = datetime(2026, 6, 16, tzinfo=timezone.utc)
        stale = supervise.classify_attempt(
            heartbeat_at=now - timedelta(minutes=25),
            started_at=now - timedelta(minutes=30),
            now=now,
            heartbeat_stale_seconds=1200,
            global_timeout_seconds=5400,
        )
        timed_out = supervise.classify_attempt(
            heartbeat_at=now - timedelta(minutes=95),
            started_at=now - timedelta(minutes=95),
            now=now,
            heartbeat_stale_seconds=1200,
            global_timeout_seconds=5400,
        )

        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["terminate"])
        self.assertEqual(timed_out["status"], "failed_timeout")
        self.assertTrue(timed_out["terminate"])

    def test_supervise_defaults_hard_timeout_to_one_hour(self):
        from robert_agent.execution import inspection as supervise

        now = datetime(2026, 10, 19, tzinfo=timezone.utc)

        running = supervise.classify_attempt(
            heartbeat_at=now,
            started_at=now - timedelta(minutes=59),
            now=now,
        )
        timed_out = supervise.classify_attempt(
            heartbeat_at=now,
            started_at=now - timedelta(hours=1),
            now=now,
        )

        self.assertEqual(running["status"], "running")
        self.assertEqual(timed_out["status"], "failed_timeout")
        self.assertEqual(timed_out["timeout_seconds"], 60 * 60)

    def test_pipeline_runtime_budget_preserves_original_heartbeat_timeout(self):
        from robert_agent.execution import inspection

        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        plan = {
            "timeout_seconds": 4200,
            "implementer": {"timeout_seconds": 4200},
            "workers": [{"timeout_seconds": 4200}],
        }
        for heartbeat_age, expected in ((0, "running"), (1200, "stale"), (4200, "failed_timeout")):
            with self.subTest(heartbeat_age=heartbeat_age):
                status = inspection.classify_attempt(
                    now - timedelta(seconds=heartbeat_age),
                    now - timedelta(seconds=5000),
                    now=now,
                    global_timeout_seconds=4200,
                    hard_timeout_seconds=4200,
                    pipeline_plan=plan,
                )
                self.assertEqual(status["status"], expected)
                self.assertEqual(status["terminate"], expected == "failed_timeout")

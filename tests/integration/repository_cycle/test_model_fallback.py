from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class ModelFallbackTests(RepositoryCycleFixture):
    def test_dispatch_uses_fallback_model_after_two_consecutive_timeouts(self):
        call = self._dispatch_model_after_failures(
            [
                {"status": "failed_timeout"},
                {"status": "failed_timeout"},
            ]
        )

        self.assertEqual(call["model"], "model-fallback")
        self.assertEqual(call["reasoning_effort"], "high")

    def test_dispatch_keeps_primary_model_before_second_consecutive_timeout(self):
        call = self._dispatch_model_after_failures([{"status": "failed_timeout"}])

        self.assertEqual(call["model"], "route-model")

    def test_dispatch_non_timeout_failure_resets_fallback_sequence(self):
        call = self._dispatch_model_after_failures(
            [
                {"status": "failed_timeout"},
                {"status": "failed_worker_startup"},
            ]
        )

        self.assertEqual(call["model"], "route-model")

    def test_dispatch_success_resets_fallback_sequence(self):
        call = self._dispatch_model_after_failures(
            [
                {"status": "failed_timeout"},
                None,
            ]
        )

        self.assertEqual(call["model"], "route-model")

    def test_dispatch_counts_recovered_timeouts_for_fallback(self):
        call = self._dispatch_model_after_failures(
            [
                {
                    "status": "needs_resume",
                    "original_status": "failed_timeout",
                },
                {"status": "failed_timeout"},
            ]
        )

        self.assertEqual(call["model"], "model-fallback")

    def test_dispatch_without_fallback_model_keeps_primary_model(self):
        call = self._dispatch_model_after_failures(
            [
                {"status": "failed_timeout"},
                {"status": "failed_timeout"},
            ],
            fallback_model=None,
        )

        self.assertEqual(call["model"], "route-model")

    def test_dispatch_same_task_resume_uses_fallback_after_two_timeouts(self):
        call = self._dispatch_model_after_failures(
            [
                {"status": "failed_timeout"},
                {"status": "failed_timeout"},
            ],
            same_task=True,
        )

        self.assertEqual(call["model"], "model-fallback")

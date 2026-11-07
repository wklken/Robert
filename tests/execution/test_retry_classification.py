"""Acceptance gaps left by the first reliability delivery."""

import unittest

from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401

from robert_agent.execution import retry as retry_policy


from robert_agent.execution.application import evidence as cycle_evidence


class RetryClassificationTests(unittest.TestCase):
    def test_terminal_rate_limit_ignores_quoted_recovered_and_superseded_errors(self):
        import json

        failed = json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}})
        quoted = json.dumps({"type": "item.completed", "item": {"aggregated_output": failed}})
        for tail in (
            quoted,
            failed + "\n" + json.dumps({"type": "turn.completed"}),
            failed + "\n" + json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized"}}),
            failed + "\n" + json.dumps({"type": "result", "is_error": False}),
        ):
            with self.subTest(tail=tail):
                self.assertIsNone(retry_policy.terminal_rate_limit({"stdout": {"tail": tail}}))
        for tail in (
            failed,
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "errors": ["429 Too Many Requests"],
                }
            ),
        ):
            self.assertIsNotNone(retry_policy.terminal_rate_limit({"stdout": {"tail": tail}}))
        self.assertIsNotNone(retry_policy.terminal_rate_limit({"stderr": {"tail": "HTTP 429 Too Many Requests"}}))

    def test_permission_403_and_rate_limit_403_are_distinct(self):
        self.assertEqual(retry_policy.error_class("HTTP 403 Forbidden"), "permission_required")
        self.assertEqual(
            retry_policy.error_class("HTTP 403: API rate limit exceeded"),
            "rate_limited",
        )
        self.assertEqual(retry_policy.error_class("HTTP 429 Too Many Requests"), "rate_limited")

    def test_retry_after_is_respected_without_extending_budget(self):
        now = "2026-10-24T00:00:00+00:00"
        state = retry_policy.failure_state({}, "HTTP 429\nRetry-After: 120", now)
        self.assertTrue(state["retryable"])
        self.assertEqual(state["next_retry_at"], "2026-10-24T00:02:00+00:00")
        state = retry_policy.failure_state({}, "HTTP 429\nRetry-After: Sat, 24 Oct 2026 00:03:00 GMT", now)
        self.assertEqual(state["next_retry_at"], "2026-10-24T00:03:00+00:00")
        state = retry_policy.failure_state({}, "HTTP 429\nRetry-After: 900", now)
        self.assertFalse(state["retryable"])
        self.assertEqual(state["reason_code"], "retry_budget_exhausted")

    def test_malformed_retry_after_does_not_crash_or_disable_budget(self):
        state = retry_policy.failure_state({}, "HTTP 429\nRetry-After: invalid", "2026-10-24T00:00:00+00:00")
        self.assertEqual(state["next_retry_at"], "2026-10-24T00:00:05+00:00")

    def test_worker_error_uses_terminal_protocol_not_quoted_content(self):
        import json

        failed = json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
        quoted = json.dumps({"type": "item.completed", "item": {"text": "401 Unauthorized"}})
        self.assertEqual(
            retry_policy.error_class(retry_policy.worker_error({"worker_stdout": {"tail": failed}})),
            "authentication_required",
        )
        self.assertEqual(retry_policy.worker_error({"worker_stdout": {"tail": quoted}}), "")
        self.assertEqual(
            retry_policy.worker_error(
                {"worker_stdout": {"tail": failed + "\n" + json.dumps({"type": "turn.completed"})}}
            ),
            "",
        )

    def test_error_only_logs_are_not_progress(self):
        with (
            patch.object(cycle_evidence, "attempt_command_records", return_value=[]),
            patch.object(cycle_evidence, "_latest_worker_phases", return_value=[]),
            patch.object(
                cycle_evidence,
                "_attempt_log_tails",
                return_value={"worker_stderr": {"tail": "HTTP 503 service unavailable"}},
            ),
            patch.object(cycle_evidence, "git_recovery_summary", return_value={}),
        ):
            evidence = cycle_evidence.inspect_attempt_recovery(None, "a", "t", None)
        self.assertEqual(evidence["status"], "no_progress")
        self.assertEqual(evidence["progress_signals"], [])

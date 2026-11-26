"""Publication retries retain provider deadlines and require proven retry facts."""

from datetime import datetime, timedelta
import unittest

from robert_agent.delivery import retry
from robert_agent.execution import retry as execution_retry


NOW = "2026-11-26T00:00:00+00:00"


def after(seconds):
    return (datetime.fromisoformat(NOW) + timedelta(seconds=seconds)).isoformat()


class PublicationRetryTests(unittest.TestCase):
    def network(self, previous=None, now=NOW, **kwargs):
        return retry.failure_state(
            {} if previous is None else previous, "connection reset", now, error_class="network_failure", **kwargs
        )

    def rate_limit(self, previous=None, now=NOW, **kwargs):
        return retry.failure_state({} if previous is None else previous, "HTTP 429", now, **kwargs)

    def test_generic_failure_keeps_shared_policy_without_automatic_authority(self):
        for previous in ({}, {"attempt_count": 1, "first_failed_at": NOW}):
            with self.subTest(previous=previous):
                state = retry.failure_state(previous, "unknown unsuccessful response", NOW)
                self.assertEqual(state, execution_retry.failure_state(previous, "unknown unsuccessful response", NOW))
                self.assertFalse(retry.automatic_retry(state, state["next_retry_at"]))

    def test_transport_message_alone_does_not_prove_a_network_failure(self):
        state = retry.failure_state({}, "connection reset", NOW)
        self.assertEqual(state["error_class"], "transient_failure")
        self.assertFalse(retry.automatic_retry(state, after(5)))

    def test_generic_failure_preserves_shared_timezone_encoding(self):
        now = "2026-11-26T08:00:00+08:00"
        self.assertEqual(
            retry.failure_state({}, "unknown failure", now),
            execution_retry.failure_state({}, "unknown failure", now),
        )

    def test_network_retry_delays_and_attempt_limit(self):
        state = self.network()
        self.assertEqual(state["error_class"], "network_failure")
        self.assertEqual(state["next_retry_at"], after(5))
        self.assertEqual(state["max_attempts"], 3)
        self.assertEqual(state["reason_code"], "retry_scheduled")
        self.assertTrue(state["retryable"])
        self.assertFalse(retry.automatic_retry(state, after(4)))
        self.assertTrue(retry.automatic_retry(state, after(5)))
        state = self.network(state, after(5))
        self.assertEqual(state["attempt_count"], 2)
        self.assertEqual(state["first_failed_at"], NOW)
        self.assertEqual(state["next_retry_at"], after(15))
        self.assertTrue(retry.automatic_retry(state, after(15)))
        state = self.network(state, after(15))
        self.assertEqual(state["attempt_count"], 3)
        self.assertEqual(state["reason_code"], "retry_budget_exhausted")
        self.assertFalse(state["retryable"])
        self.assertIsNone(state["next_retry_at"])
        self.assertIsNotNone(state["required_action"])
        self.assertFalse(retry.automatic_retry(state, after(20)))

    def test_network_elapsed_budget_checks_the_scheduled_deadline(self):
        prior = {"attempt_count": 0, "first_failed_at": NOW}
        self.assertEqual(self.network(prior, after(594))["next_retry_at"], after(599))
        self.assertFalse(self.network(prior, after(595))["retryable"])
        self.assertFalse(retry.automatic_retry(self.network(), after(600)))

    def test_retryable_false_vetoes_network_and_rate_limit(self):
        for state in (self.network(retryable=False), self.rate_limit(retryable=False)):
            with self.subTest(state=state):
                self.assertFalse(state["retryable"])
                self.assertIsNone(state["next_retry_at"])
                self.assertFalse(retry.automatic_retry(state, after(120)))

    def test_permanent_failures_preserve_shared_fields(self):
        for message in ("HTTP 401 Unauthorized", "HTTP 403 Forbidden", "non-fast-forward", "invalid configuration"):
            with self.subTest(message=message):
                state = retry.failure_state({}, message, NOW)
                self.assertEqual(state, execution_retry.failure_state({}, message, NOW))
                self.assertFalse(retry.automatic_retry(state, after(60)))

    def test_http_status_distinguishes_normal_403_rate_limit_403_and_429(self):
        normal = retry.failure_state({}, "request rejected", NOW, http_status=403)
        self.assertEqual(normal["error_class"], "permission_required")
        self.assertFalse(normal["retryable"])
        for status, message, headers, category in (
            (403, "secondary rate limit", None, None),
            (403, "request rejected", {"X-RateLimit-Remaining": "0"}, None),
            (403, "request rejected", {"Retry-After": "90"}, None),
            (403, "request rejected", None, "rate_limited"),
            (429, "request rejected", None, None),
        ):
            with self.subTest(status=status, headers=headers):
                state = retry.failure_state(
                    {}, message, NOW, http_status=status, response_headers=headers, error_class=category
                )
                self.assertEqual(state["error_class"], "rate_limited")
                self.assertTrue(state["retryable"])

    def test_unexhausted_primary_limit_does_not_turn_403_into_rate_limit(self):
        for remaining in ("1", "-1", "invalid", None, True):
            with self.subTest(remaining=remaining):
                state = retry.failure_state(
                    {},
                    "Forbidden",
                    NOW,
                    http_status=403,
                    response_headers={"X-RateLimit-Remaining": remaining, "X-RateLimit-Reset": "999999999999999999"},
                )
                self.assertEqual(state["error_class"], "permission_required")
                self.assertFalse(state["retryable"])

    def test_retry_after_decimal_seconds_and_case_insensitive_header(self):
        state = self.rate_limit(response_headers={"rEtRy-AfTeR": " 120 "})
        self.assertEqual(state["next_retry_at"], after(120))
        self.assertEqual(state["retry_not_before"], after(120))
        self.assertFalse(retry.automatic_retry(state, after(119)))
        self.assertTrue(retry.automatic_retry(state, after(120)))

    def test_fractional_retry_after_seconds_are_preserved(self):
        state = self.rate_limit(response_headers={"Retry-After": "120.5"})
        self.assertEqual(state["retry_not_before"], after(120.5))
        self.assertEqual(state["next_retry_at"], after(120.5))

    def test_retry_after_http_date_uses_utc(self):
        state = self.rate_limit(
            now="2026-11-26T08:00:00+08:00",
            response_headers={"Retry-After": "Thu, 26 Nov 2026 00:03:00 GMT"},
        )
        self.assertEqual(state["retry_not_before"], after(180))
        self.assertEqual(state["next_retry_at"], after(180))

    def test_embedded_retry_after_remains_supported(self):
        state = retry.failure_state({}, "HTTP 429\nRetry-After: 900", NOW)
        self.assertTrue(state["retryable"])
        self.assertEqual(state["next_retry_at"], after(900))
        self.assertEqual(state["retry_not_before"], after(900))

    def test_primary_exhaustion_uses_reset_epoch_with_one_second_margin(self):
        reset = int(datetime.fromisoformat(after(900)).timestamp())
        state = retry.failure_state(
            {},
            "request rejected",
            NOW,
            http_status=403,
            response_headers={"X-RATELIMIT-REMAINING": "0", "x-RATELIMIT-reset": str(reset)},
        )
        self.assertEqual(state["next_retry_at"], after(901))
        self.assertEqual(state["retry_not_before"], after(901))
        self.assertFalse(retry.automatic_retry(state, after(900)))
        self.assertTrue(retry.automatic_retry(state, after(901)))

    def test_retry_after_and_reset_respect_the_later_deadline(self):
        for retry_after, reset_delay, expected in ((120, 180, 181), (240, 180, 240)):
            with self.subTest(retry_after=retry_after):
                reset = int(datetime.fromisoformat(after(reset_delay)).timestamp())
                state = self.rate_limit(
                    response_headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset),
                    }
                )
                self.assertEqual(state["next_retry_at"], after(expected))
                self.assertEqual(state["retry_not_before"], after(expected))

    def test_reset_is_ignored_unless_primary_limit_is_exhausted(self):
        reset = int(datetime.fromisoformat(after(900)).timestamp())
        for remaining in (None, "1", "invalid"):
            with self.subTest(remaining=remaining):
                state = self.rate_limit(
                    response_headers={
                        "Retry-After": "120",
                        "X-RateLimit-Remaining": remaining,
                        "X-RateLimit-Reset": str(reset),
                    }
                )
                self.assertEqual(state["next_retry_at"], after(120))

    def test_secondary_without_hints_waits_at_least_a_minute_then_increases(self):
        state = self.rate_limit()
        self.assertEqual(state["next_retry_at"], after(60))
        self.assertEqual(state["retry_not_before"], after(60))
        state = self.rate_limit(state, after(60))
        self.assertEqual(state["next_retry_at"], after(180))
        self.assertEqual(state["first_failed_at"], NOW)
        self.assertTrue(retry.automatic_retry(state, after(180)))
        self.assertFalse(self.rate_limit(state, after(180))["retryable"])

    def test_provider_wait_beyond_elapsed_budget_retains_attempt_budget(self):
        headers = {"Retry-After": "900"}
        state = self.rate_limit(response_headers=headers)
        self.assertEqual(state["next_retry_at"], after(900))
        self.assertFalse(retry.automatic_retry(state, after(600)))
        self.assertTrue(retry.automatic_retry(state, after(900)))
        state = self.rate_limit(state, after(900), response_headers=headers)
        self.assertEqual(state["attempt_count"], 2)
        self.assertEqual(state["first_failed_at"], NOW)
        self.assertEqual(state["next_retry_at"], after(1800))
        self.assertTrue(retry.automatic_retry(state, after(1800)))
        state = self.rate_limit(state, after(1800), response_headers=headers)
        self.assertFalse(state["retryable"])
        self.assertEqual(state["attempt_count"], 3)
        self.assertEqual(state["retry_not_before"], after(2700))

    def test_provider_wait_does_not_consume_the_next_network_retry_window(self):
        prior = self.rate_limit(response_headers={"Retry-After": "900"})
        state = self.network(prior, after(900))
        self.assertEqual(state["attempt_count"], 2)
        self.assertEqual(state["first_failed_at"], NOW)
        self.assertEqual(state["retry_not_before"], after(900))
        self.assertTrue(state["retryable"])
        self.assertEqual(state["next_retry_at"], after(910))
        self.assertFalse(retry.automatic_retry(state, after(909)))
        self.assertTrue(retry.automatic_retry(state, after(910)))
        self.assertFalse(retry.retry_budget_exhausted(state, after(910)))
        final = self.network(state, after(910))
        self.assertEqual(final["attempt_count"], 3)
        self.assertFalse(final["retryable"])
        self.assertIsNone(final["next_retry_at"])
        self.assertEqual(final["retry_not_before"], after(900))
        self.assertTrue(retry.retry_budget_exhausted(final, after(910)))

    def test_network_window_after_provider_wait_is_bounded_from_the_deadline(self):
        prior = self.rate_limit(response_headers={"Retry-After": "900"})
        last_eligible = self.network(prior, after(1489))
        self.assertEqual(last_eligible["next_retry_at"], after(1499))
        self.assertTrue(retry.automatic_retry(last_eligible, after(1499)))
        self.assertFalse(retry.automatic_retry(last_eligible, after(1500)))
        self.assertTrue(retry.retry_budget_exhausted(last_eligible, after(1500)))
        exhausted = self.network(prior, after(1490))
        self.assertFalse(exhausted["retryable"])
        self.assertIsNone(exhausted["next_retry_at"])
        self.assertEqual(exhausted["retry_not_before"], after(900))

    def test_final_rate_limit_failure_retains_account_cooldown_without_retry(self):
        prior = self.network()
        prior = self.network(prior, after(5))
        final = self.rate_limit(prior, after(15), response_headers={"Retry-After": "900"})
        self.assertEqual(final["attempt_count"], 3)
        self.assertFalse(final["retryable"])
        self.assertIsNone(final["next_retry_at"])
        self.assertEqual(final["retry_not_before"], after(915))
        self.assertFalse(retry.automatic_retry(final, after(915)))

    def test_reconciliation_read_retries_preserve_original_write_eligibility_and_budget(self):
        original = self.network(self.rate_limit(response_headers={"Retry-After": "900"}), after(900))
        saved = dict(original)
        read = retry.failure_state({}, "connection reset during read", after(910), error_class="network_failure")
        for expected_count in (2, 3):
            read = self.network(read, read["next_retry_at"])
            self.assertEqual(read["attempt_count"], expected_count)
            self.assertEqual(read["first_failed_at"], after(910))
            self.assertEqual(original, saved)
        self.assertFalse(read["retryable"])
        self.assertEqual(original["attempt_count"], 2)
        self.assertEqual(original["first_failed_at"], NOW)
        self.assertTrue(retry.automatic_retry(original, after(925)))
        self.assertFalse(retry.retry_budget_exhausted(original, after(925)))

    def test_nested_reconcile_provider_wait_allows_read_then_original_write(self):
        original = self.network()
        read = self.rate_limit(now=after(5), response_headers={"Retry-After": "1800"})
        scheduled = {
            **original,
            "reconcile_required": True,
            "reconcile_retry": read,
            "next_retry_at": read["next_retry_at"],
        }
        self.assertFalse(retry.automatic_retry(scheduled, after(1804)))
        self.assertTrue(retry.automatic_retry(scheduled, after(1805)))
        self.assertFalse(retry.retry_budget_exhausted(scheduled, after(1805)))
        absent = {**scheduled, "reconcile_required": False}
        self.assertTrue(retry.automatic_retry(absent, after(1805)))
        next_failure = self.network(absent, after(1805))
        self.assertEqual(next_failure["attempt_count"], 2)
        self.assertEqual(next_failure["first_failed_at"], NOW)
        self.assertEqual(next_failure["next_retry_at"], after(1815))
        self.assertEqual(next_failure["reconcile_retry"], read)
        self.assertEqual(next_failure["provider_wait_seconds"], 1800)
        self.assertEqual(next_failure["reconcile_provider_wait_seconds"], 1800)
        self.assertTrue(retry.automatic_retry(next_failure, after(1815)))
        self.assertTrue(retry.retry_budget_exhausted(next_failure, after(2400)))

    def test_provider_wait_seconds_accumulate_without_resetting_active_elapsed(self):
        original = {"attempt_count": 0, "first_failed_at": NOW}
        first = self.rate_limit(original, after(100), response_headers={"Retry-After": "900"})
        self.assertEqual(first["provider_wait_seconds"], 900)
        second = self.network(first, after(1000))
        self.assertEqual(second["provider_wait_seconds"], 900)
        self.assertFalse(retry.retry_budget_exhausted(second, after(1499)))
        self.assertTrue(retry.retry_budget_exhausted(second, after(1500)))
        repeated = self.rate_limit(
            self.rate_limit(response_headers={"Retry-After": "900"}),
            after(900),
            response_headers={"Retry-After": "1800"},
        )
        self.assertEqual(repeated["provider_wait_seconds"], 2700)

    def test_overlapping_provider_hints_account_only_the_extension(self):
        first = self.rate_limit(response_headers={"Retry-After": "900"})
        second = self.rate_limit(first, after(100), response_headers={"Retry-After": "1200"})
        self.assertEqual(second["provider_wait_seconds"], 1300)
        shorter = self.rate_limit(first, after(100), response_headers={"Retry-After": "10"})
        self.assertEqual(shorter["provider_wait_seconds"], 900)

    def test_nested_provider_wait_is_accounted_once_across_failure_recording(self):
        original = self.rate_limit(response_headers={"Retry-After": "900"})
        read = self.rate_limit(now=after(900), response_headers={"Retry-After": "1800"})
        prior = {
            **original,
            "reconcile_retry": read,
            "reconcile_required": False,
            "next_retry_at": read["next_retry_at"],
        }
        failed = self.network(prior, after(2700))
        self.assertEqual(failed["provider_wait_seconds"], 2700)
        self.assertEqual(failed["reconcile_provider_wait_seconds"], 1800)
        self.assertEqual(failed["reconcile_retry"]["provider_wait_seconds"], 1800)
        self.assertFalse(retry.retry_budget_exhausted(failed, after(3299)))
        self.assertTrue(retry.retry_budget_exhausted(failed, after(3300)))
        self.assertEqual(failed["first_failed_at"], NOW)
        final = self.network(failed, after(2710))
        self.assertEqual(final["provider_wait_seconds"], 2700)
        self.assertEqual(final["reconcile_provider_wait_seconds"], 1800)
        self.assertEqual(final["reconcile_retry"], read)

    def test_nested_read_retry_never_grants_unknown_or_exhausted_original_write(self):
        original = self.network()
        read = self.rate_limit(now=after(5), response_headers={"Retry-After": "1800"})
        prior = {
            **original,
            "reconcile_required": True,
            "reconcile_retry": read,
            "next_retry_at": read["next_retry_at"],
        }
        for changes in (
            {"attempt_count": 3},
            {"retryable": False},
            {"error_class": "transient_failure"},
            {"reconcile_retry": {**read, "attempt_count": 3}},
            {"reconcile_retry": {**read, "retryable": False}},
            {"reconcile_retry": {**read, "error_class": "transient_failure"}},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(retry.automatic_retry({**prior, **changes}, after(1805)))

    def test_malformed_wait_counters_and_recursive_read_metadata_fail_closed(self):
        original = self.network()
        read = self.rate_limit(now=after(5), response_headers={"Retry-After": "1800"})
        for value in (-1, True, "1800", float("inf"), float("nan"), 10**100, 2000):
            with self.subTest(value=value):
                prior = {**original, "provider_wait_seconds": value}
                self.assertFalse(retry.automatic_retry(prior, after(5)))
                self.assertTrue(retry.retry_budget_exhausted(prior, after(5)))
                self.assertFalse(self.network(prior, after(5))["retryable"])
        for nested in (None, [], {**read, "provider_wait_seconds": -1}, {**read, "reconcile_retry": read}):
            with self.subTest(nested=nested):
                prior = {**original, "reconcile_retry": nested}
                self.assertFalse(retry.automatic_retry(prior, after(1805)))
                self.assertTrue(retry.retry_budget_exhausted(prior, after(1805)))

    def test_reconciliation_plain_network_backoff_does_consume_elapsed_budget(self):
        original = self.network()
        read = self.network(now=after(5))
        prior = {**original, "reconcile_required": True, "reconcile_retry": read}
        self.assertTrue(retry.automatic_retry(prior, after(10)))
        self.assertFalse(retry.automatic_retry(prior, after(600)))
        self.assertTrue(retry.retry_budget_exhausted(prior, after(600)))

    def test_nested_rate_limit_at_one_second_is_native_ready_at_1801(self):
        original = self.network()
        read = self.rate_limit(now=after(1), response_headers={"Retry-After": "1800"})
        scheduled = {
            **original,
            "reconcile_retry": read,
            "reconcile_required": True,
            "next_retry_at": read["next_retry_at"],
        }
        self.assertFalse(retry.automatic_retry(scheduled, after(1800)))
        self.assertTrue(retry.automatic_retry(scheduled, after(1801)))
        self.assertFalse(retry.retry_budget_exhausted(scheduled, after(1801)))
        self.assertTrue(retry.automatic_retry({**scheduled, "reconcile_required": False}, after(1801)))

    def test_new_nested_wait_only_adds_the_new_read_delay(self):
        original = self.network()
        read = self.rate_limit(now=after(5), response_headers={"Retry-After": "1800"})
        prior = {**original, "reconcile_retry": read, "reconcile_required": False}
        failed = self.network(prior, after(1805))
        self.assertEqual(failed["provider_wait_seconds"], 1800)
        read = self.rate_limit(read, after(1815), response_headers={"Retry-After": "900"})
        updated = {
            **failed,
            "reconcile_retry": read,
            "reconcile_required": True,
            "next_retry_at": read["next_retry_at"],
        }
        self.assertTrue(retry.automatic_retry(updated, after(2715)))
        final = self.network({**updated, "reconcile_required": False}, after(2715))
        self.assertEqual(final["provider_wait_seconds"], 2700)
        self.assertEqual(final["reconcile_provider_wait_seconds"], 2700)
        self.assertEqual(final["attempt_count"], 3)
        self.assertEqual(final["reconcile_retry"]["attempt_count"], 2)
        self.assertIsNone(final["next_retry_at"])

    def test_overlapping_parent_and_nested_wait_are_counted_once(self):
        original = self.rate_limit(response_headers={"Retry-After": "900"})
        read = self.rate_limit(now=after(100), response_headers={"Retry-After": "900"})
        prior = {**original, "reconcile_retry": read, "reconcile_required": False}
        failed = self.network(prior, after(1000))
        self.assertEqual(failed["provider_wait_seconds"], 1000)
        self.assertEqual(failed["reconcile_provider_wait_seconds"], 100)
        self.assertFalse(retry.retry_budget_exhausted(failed, after(1599)))
        self.assertTrue(retry.retry_budget_exhausted(failed, after(1600)))

    def test_malformed_accounted_nested_wait_fails_closed(self):
        read = self.rate_limit(now=after(5), response_headers={"Retry-After": "1800"})
        prior = {**self.network(), "reconcile_retry": read, "provider_wait_seconds": 1800}
        for marker in (-1, True, "1800", float("nan"), 1801):
            with self.subTest(marker=marker):
                corrupt = {**prior, "reconcile_provider_wait_seconds": marker}
                self.assertFalse(retry.automatic_retry(corrupt, after(1805)))
                self.assertTrue(retry.retry_budget_exhausted(corrupt, after(1805)))

    def test_previous_provider_deadline_survives_a_shorter_hint_and_later_gate(self):
        prior = self.rate_limit(response_headers={"Retry-After": "900"})
        state = self.rate_limit(prior, after(1), response_headers={"Retry-After": "10"})
        self.assertEqual(state["retry_not_before"], after(900))
        self.assertEqual(state["next_retry_at"], after(900))
        generic = retry.failure_state(prior, "unknown failure", after(1))
        self.assertEqual(generic["retry_not_before"], after(900))
        self.assertGreaterEqual(datetime.fromisoformat(generic["next_retry_at"]), datetime.fromisoformat(after(900)))
        self.assertFalse(retry.automatic_retry(generic, after(900)))

    def test_invalid_retry_after_headers_are_ignored(self):
        for value in (
            None,
            True,
            -1,
            [],
            {},
            "",
            "-1",
            "NaN",
            "inf",
            "9" * 5000,
            "Thu, 26 Nov 999999 00:00:00 GMT",
            "Thu, 26 Nov 2026 00:00:00 BAD",
            "120\r\nAuthorization: secret",
        ):
            with self.subTest(value=repr(value)[:80]):
                state = self.rate_limit(response_headers={"Retry-After": value})
                self.assertEqual(state["next_retry_at"], after(60))
                self.assertTrue(state["retryable"])

    def test_invalid_reset_headers_are_ignored(self):
        for value in (None, True, -1, [], "-1", "NaN", "1e20", "9" * 5000, "253402300799"):
            with self.subTest(value=repr(value)[:80]):
                state = self.rate_limit(response_headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": value})
                self.assertEqual(state["next_retry_at"], after(60))

    def test_invalid_header_containers_and_names_are_ignored(self):
        for headers in (None, [], "Retry-After: 900", 1, {1: "900"}, {"Retry-After\r\n": "900"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.rate_limit(response_headers=headers)["next_retry_at"], after(60))

    def test_past_headers_do_not_schedule_in_the_past(self):
        state = self.rate_limit(response_headers={"Retry-After": "Wed, 25 Nov 2026 23:59:00 GMT"})
        self.assertGreater(datetime.fromisoformat(state["next_retry_at"]), datetime.fromisoformat(NOW))

    def test_malformed_previous_fails_closed_without_resetting_budget(self):
        malformed = (
            None,
            [],
            "broken",
            {"attempt_count": -1},
            {"attempt_count": True},
            {"attempt_count": 1.5},
            {"attempt_count": "1"},
            {"attempt_count": 10**100},
            {"attempt_count": 1},
            {"attempt_count": 1, "first_failed_at": "broken"},
            {"attempt_count": 1, "first_failed_at": after(1)},
            {"attempt_count": 1, "first_failed_at": NOW, "next_retry_at": "broken"},
            {"attempt_count": 1, "first_failed_at": NOW, "retry_not_before": "broken"},
            {"attempt_count": 1, "first_failed_at": NOW, "max_attempts": 100},
        )
        for previous in malformed:
            with self.subTest(previous=previous):
                state = retry.failure_state(previous, "connection reset", NOW, error_class="network_failure")
                self.assertFalse(state["retryable"])
                self.assertIsNone(state["next_retry_at"])
                self.assertFalse(retry.automatic_retry(state, after(5)))

    def test_automatic_retry_rejects_corrupt_and_legacy_executing_metadata(self):
        valid = self.network()
        malformed = (None, [], {}, {"status": "executing"}, {"attempt_count": 1, "attempt_charged": True})
        for previous in malformed:
            with self.subTest(previous=previous):
                self.assertFalse(retry.automatic_retry(previous, after(5)))
        changes = (
            {"error_class": "transient_failure"},
            {"error_class": None},
            {"retryable": 1},
            {"retryable": "true"},
            {"retryable": False},
            {"attempt_count": -1},
            {"attempt_count": 0},
            {"attempt_count": 3},
            {"attempt_count": True},
            {"attempt_count": "1"},
            {"attempt_count": 1.5},
            {"attempt_count": 10**100},
            {"max_attempts": 100},
            {"max_attempts": True},
            {"next_retry_at": None},
            {"next_retry_at": "broken"},
            {"next_retry_at": "999999-01-01T00:00:00+00:00"},
            {"next_retry_at": after(-1)},
            {"first_failed_at": None},
            {"first_failed_at": "broken"},
            {"first_failed_at": after(10)},
            {"failed_at": "broken"},
            {"failed_at": after(-1)},
            {"retry_not_before": "broken"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(retry.automatic_retry({**valid, **change}, after(5)))

    def test_non_string_stored_classifications_fail_closed(self):
        valid = self.network()
        for category in ([], {}, True, 1):
            with self.subTest(category=category):
                state = {**valid, "error_class": category}
                self.assertFalse(retry.automatic_retry(state, after(5)))
                self.assertTrue(retry.retry_budget_exhausted(state, after(5)))
                self.assertFalse(retry.failure_state(state, "HTTP 429", after(5))["retryable"])

    def test_timestamp_overflow_fails_closed(self):
        now = "9999-12-31T23:59:59+00:00"
        for message, category in (("unknown", None), ("HTTP 429", None), ("connection reset", "network_failure")):
            with self.subTest(category=category):
                state = retry.failure_state({}, message, now, error_class=category)
                self.assertFalse(state["retryable"])
                self.assertIsNone(state["next_retry_at"])

    def test_unknown_explicit_classification_never_stores_arbitrary_text(self):
        state = retry.failure_state({}, "unknown failure", NOW, error_class="credential=secret")
        self.assertEqual(state["error_class"], "transient_failure")
        self.assertNotIn("secret", repr(state))

    def test_automatic_retry_honors_provider_floor_even_if_next_date_is_earlier(self):
        state = self.rate_limit(response_headers={"Retry-After": "900"})
        state["next_retry_at"] = after(5)
        self.assertFalse(retry.automatic_retry(state, after(5)))
        self.assertTrue(retry.automatic_retry(state, after(900)))

    def test_invalid_now_fails_closed(self):
        for now in (None, "broken", "2026-11-26", "2026-11-26T00:00:00", "999999-01-01T00:00:00+00:00"):
            with self.subTest(now=now):
                self.assertFalse(retry.failure_state({}, "HTTP 429", now)["retryable"])
                self.assertFalse(retry.automatic_retry(self.network(), now))

    def test_reconciliation_can_use_the_same_helper_with_its_own_prior_budget(self):
        publication = self.network()
        read = self.network()
        for expected_count in (2, 3):
            read = self.network(read, read["next_retry_at"])
            self.assertEqual(read["attempt_count"], expected_count)
            self.assertEqual(read["first_failed_at"], NOW)
        self.assertFalse(read["retryable"])
        self.assertEqual(publication["attempt_count"], 1)

    def test_budget_helper_keeps_legacy_elapsed_and_three_actual_attempts(self):
        self.assertFalse(retry.retry_budget_exhausted({}, NOW))
        self.assertFalse(retry.retry_budget_exhausted({"attempt_count": 2, "first_failed_at": NOW}, after(599)))
        self.assertTrue(retry.retry_budget_exhausted({"attempt_count": 3, "first_failed_at": NOW}, NOW))
        self.assertTrue(retry.retry_budget_exhausted({"attempt_count": 0, "first_failed_at": NOW}, after(600)))
        self.assertTrue(retry.retry_budget_exhausted(self.network(), after(600)))

    def test_budget_helper_keeps_long_provider_wait_eligible_without_reset(self):
        state = self.rate_limit(response_headers={"Retry-After": "900"})
        self.assertFalse(retry.retry_budget_exhausted(state, after(600)))
        self.assertFalse(retry.retry_budget_exhausted(state, after(900)))
        self.assertFalse(retry.retry_budget_exhausted(state, after(901)))
        state = self.rate_limit(state, after(900), response_headers={"Retry-After": "900"})
        self.assertFalse(retry.retry_budget_exhausted(state, after(1800)))
        exhausted = self.rate_limit(state, after(1800))
        self.assertTrue(retry.retry_budget_exhausted(exhausted, after(1800)))

    def test_provider_wait_survives_precharged_second_write_and_exhausts_third(self):
        prior = self.rate_limit(response_headers={"Retry-After": "900"})
        charged = {**prior, "attempt_count": 2, "attempt_charged": True, "last_attempt_at": after(900)}
        self.assertFalse(retry.retry_budget_exhausted(charged, after(900)))
        # Failure recording reverses only the already charged count before invoking the helper.
        failed = self.rate_limit({**charged, "attempt_count": 1}, after(900), response_headers={"Retry-After": "900"})
        self.assertEqual(failed["attempt_count"], 2)
        self.assertEqual(failed["first_failed_at"], NOW)
        self.assertTrue(retry.automatic_retry(failed, after(1800)))
        third = {**failed, "attempt_count": 3, "attempt_charged": True, "last_attempt_at": after(1800)}
        self.assertTrue(retry.retry_budget_exhausted(third, after(1800)))

    def test_budget_helper_fails_closed_on_corrupt_metadata(self):
        for prior in (
            None,
            [],
            {"attempt_count": -1},
            {"attempt_count": True},
            {"attempt_count": "2"},
            {"attempt_count": 10**100},
            {"attempt_count": 1},
            {"first_failed_at": "broken"},
            {"retry_not_before": "broken"},
        ):
            with self.subTest(prior=prior):
                self.assertTrue(retry.retry_budget_exhausted(prior, NOW))

    def test_budget_helper_does_not_let_generic_or_legacy_metadata_claim_provider_exemption(self):
        valid = self.rate_limit(response_headers={"Retry-After": "900"})
        for changes in (
            {"error_class": "transient_failure"},
            {"retryable": False},
            {"next_retry_at": None},
            {"retry_not_before": None},
            {"error_class": None},
            {"attempt_count": 0},
        ):
            with self.subTest(changes=changes):
                self.assertTrue(retry.retry_budget_exhausted({**valid, **changes}, after(900)))

    def test_input_metadata_is_unchanged_and_headers_or_messages_are_not_retained(self):
        prior = self.rate_limit(response_headers={"Retry-After": "900"})
        saved = dict(prior)
        headers = {"Retry-After": "120", "Authorization": "secret", "Cookie": "private"}
        state = retry.failure_state(prior, "HTTP 429 credential=secret", after(1), response_headers=headers)
        self.assertEqual(prior, saved)
        self.assertEqual(headers["Authorization"], "secret")
        self.assertNotIn("secret", repr(state))
        self.assertNotIn("private", repr(state))
        self.assertEqual(
            set(state),
            set(execution_retry.failure_state({}, "unknown", NOW)) | {"retry_not_before", "provider_wait_seconds"},
        )


if __name__ == "__main__":
    unittest.main()

"""Metered activity distinguishes parallel work, queues and late registration."""

import json

from robert_agent.readmodels import usage
from tests.support.temp_workspace import TemporaryWorkspace


class UsageActivityTests(TemporaryWorkspace):
    def test_claude_input_buckets_and_partial_sample_count(self):
        payload = {
            "source": "claude_stream_json",
            "usage_available": True,
            "measurement_status": "partial",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        }
        result = usage.summarize_receipt_usage(self.root, [], recorded={("diagnosis", "call"): payload})
        self.assertEqual(result["input_tokens"], 15)
        self.assertEqual(result["uncached_input_tokens"], 10)
        self.assertEqual(result["billing_token_run_count"], 0)

    def test_activity_uses_provider_duration_and_keeps_late_registration_separate(self):
        segment = {
            "launched": True,
            "duration_ms": 600000,
            "events": {
                "queued": {"at": "2026-11-24T00:00:00Z"},
                "started": {"at": "2026-11-24T00:01:00Z"},
                "process_exit_observed": {"at": "2026-11-24T02:11:00Z"},
                "registered": {"at": "2026-11-24T02:12:00Z"},
            },
        }
        result = usage.summarize_call_activity([{"segments": {"one": segment, "two": segment}}])
        self.assertEqual(result["activity_seconds"], 1200)
        self.assertEqual(result["activity_union_seconds"], 600)
        self.assertEqual(result["queue_seconds"], 120)
        self.assertEqual(result["post_model_seconds"], 14520)

    def test_unknown_exit_keeps_activity_unknown_and_observation_visible(self):
        result = usage.summarize_call_activity(
            [
                {
                    "segments": {
                        "one": {
                            "launched": True,
                            "events": {
                                "started": {"at": "2026-11-24T00:00:00Z"},
                                "process_exit_observed": {"at": "2026-11-24T02:00:00Z"},
                            },
                        }
                    }
                }
            ]
        )
        self.assertIsNone(result["activity_seconds"])
        self.assertEqual(result["unknown_activity_count"], 1)

    def test_stage_activity_and_queue_waiting_have_separate_projections(self):
        waiting = {"launched": False, "stage": "review", "events": {"queued": {"at": "2026-11-24T00:00:00Z"}}}
        working = {
            "launched": True,
            "stage": "review",
            "duration_ms": 600000,
            "events": {"queued": {"at": "2026-11-24T00:00:00Z"}, "started": {"at": "2026-11-24T00:01:00Z"}},
        }
        result = usage.summarize_call_activity([{"segments": {"waiting": waiting, "working": working}}])
        self.assertEqual(result["by_stage"]["review"]["activity_seconds"], 600)
        self.assertEqual(result["by_stage"]["review"]["queue_seconds"], 60)
        self.assertEqual(result["queued_segment_count"], 1)

    def test_pending_receipt_counts_call_and_prevents_log_rescan(self):
        attempt = {
            "attempt_id": "call",
            "metadata_json": json.dumps({"dispatch": {}}),
            "lineage": "request",
            "started_at": "2026-11-24T00:00:00Z",
            "finished_at": None,
        }
        result = usage.summarize_receipt_usage(
            self.root,
            [(attempt, [])],
            recorded={
                ("worker", "call"): {
                    "measurement_status": "pending",
                    "usage_available": False,
                    "segments": {"call": {"launched": True}},
                }
            },
        )
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(result["pending_run_count"], 1)
        self.assertIsNone(result["input_tokens"])

    def test_startup_receipt_counts_before_attempt_start_projection_is_saved(self):
        attempt = {
            "attempt_id": "call",
            "metadata_json": "{}",
            "lineage": "request",
            "started_at": None,
            "finished_at": None,
        }
        result = usage.summarize_receipt_usage(
            self.root,
            [(attempt, [])],
            recorded={
                ("worker", "call"): {
                    "measurement_status": "pending",
                    "usage_available": False,
                    "segments": {"call": {"launched": True, "measurement_status": "pending"}},
                }
            },
        )
        self.assertEqual(result["run_count"], 1)

    def test_adoption_event_without_new_call_preserves_historical_model_invocation(self):
        run = {"run_id": "run", "dispatch_json": "{}", "started_at": "2026-11-24T00:00:00Z"}
        attempt = {
            "attempt_id": "wrapper",
            "metadata_json": json.dumps({"dispatch": {"model_coordinator": True}}),
            "lineage": "request",
            "started_at": "2026-11-24T00:00:00Z",
            "finished_at": None,
        }
        result = usage.summarize_receipt_usage(
            self.root,
            [(attempt, [run])],
            recorded={
                ("stage", "run"): {
                    "segments": {
                        "run": {"launched": False, "events": {"artifact_adopted": {"at": "2026-11-24T02:00:00Z"}}}
                    }
                }
            },
        )
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(result["unavailable_run_count"], 1)

    def test_queued_run_reservation_timestamp_does_not_count_as_launch(self):
        run = {"run_id": "run", "dispatch_json": "{}", "started_at": "2026-11-24T00:00:00Z"}
        attempt = {
            "attempt_id": "wrapper",
            "metadata_json": json.dumps({"dispatch": {"model_coordinator": True}}),
            "lineage": "request",
            "started_at": "2026-11-24T00:00:00Z",
            "finished_at": None,
        }
        result = usage.summarize_receipt_usage(
            self.root,
            [(attempt, [run])],
            recorded={
                ("stage", "run"): {
                    "segments": {"run": {"launched": False, "events": {"queued": {"at": "2026-11-24T00:00:00Z"}}}}
                }
            },
        )
        self.assertEqual(result["run_count"], 0)

    def test_legacy_totals_use_same_claude_input_buckets_as_receipt(self):
        result = usage.summarize_usage_payloads(
            [
                {
                    "usage_available": True,
                    "source": "claude_stream_json",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 2,
                    },
                }
            ]
        )
        self.assertEqual(result["total_input_tokens"], 15)

"""Provider receipts aggregate completed turns and bounded partial evidence."""

import json
from unittest.mock import patch

from robert_agent.adapters.processes import model_usage
from tests.support.temp_workspace import TemporaryWorkspace


class ModelUsageTests(TemporaryWorkspace):
    def observe(self, events, agent="tcodex", **dispatch):
        path = self.root / "stdout.log"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        return model_usage.read_dispatch(self.root, {"stdout_path": str(path), "worker_agent": agent, **dispatch})

    def test_claude_source_comes_from_adapter(self):
        for agent in ("claude", "tclaude", "cbc"):
            with self.subTest(agent=agent):
                observed = self.observe([{"type": "result", "usage": {"input_tokens": 10}}], agent)
                self.assertEqual(observed["source"], "claude_stream_json" if agent != "cbc" else "cbc_stream_json")

    def test_all_codex_turns_and_duplicate_turn_identity(self):
        first = {"type": "turn.completed", "turn_id": "one", "usage": {"input_tokens": 10, "output_tokens": 2}}
        observed = self.observe(
            [
                first,
                first,
                {"type": "turn.completed", "turn_id": "two", "usage": {"input_tokens": 15, "output_tokens": 3}},
            ]
        )
        self.assertEqual(observed["usage"], {"input_tokens": 25, "output_tokens": 5})

    def test_codex_native_cumulative_partial_reset_and_resume_baseline(self):
        def tokens(count):
            return {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": count, "output_tokens": count // 10}},
                },
            }

        observed = self.observe(
            [tokens(100), tokens(160)], usage_baseline={"usage": {"input_tokens": 100, "output_tokens": 10}}
        )
        self.assertEqual(observed["usage"]["input_tokens"], 60)
        self.assertEqual(observed["measurement_status"], "partial")
        reset = self.observe([tokens(100), tokens(20), tokens(35)])
        self.assertEqual(reset["usage"]["input_tokens"], 135)
        self.assertEqual(reset["counter_resets"], 1)

    def test_launch_delta_does_not_subtract_session_baseline(self):
        observed = self.observe(
            [{"type": "turn.completed", "usage": {"input_tokens": 60, "output_tokens": 4}}],
            usage_baseline={"usage": {"input_tokens": 100}},
        )
        self.assertEqual(observed["usage"]["input_tokens"], 60)

    def test_claude_launch_cumulative_does_not_subtract_prior_session(self):
        observed = self.observe(
            [{"type": "result", "usage": {"input_tokens": 160, "output_tokens": 4}}],
            "tclaude",
            usage_baseline={"usage": {"input_tokens": 100}},
        )
        self.assertEqual(observed["usage"]["input_tokens"], 160)

    def test_cumulative_duplicate_snapshot_cost_reset_and_aliases(self):
        first = {"type": "result", "usage": {"inputTokens": 100, "outputTokens": 10}, "total_cost_usd": 1.0}
        second = {"type": "result", "usage": {"inputTokens": 20, "outputTokens": 2}, "total_cost_usd": 0.2}
        observed = self.observe([first, first, second], "cbc")
        self.assertEqual(observed["usage"]["input_tokens"], 120)
        self.assertEqual(observed["usage"]["output_tokens"], 12)
        self.assertAlmostEqual(observed["total_cost_usd"], 1.2)

    def test_claude_assistant_partial_is_replaced_by_authoritative_result(self):
        assistant = {
            "type": "assistant",
            "message": {"id": "message-one", "usage": {"input_tokens": 8, "output_tokens": 2}},
        }
        partial = self.observe([assistant, assistant], "tclaude")
        self.assertEqual(partial["usage"]["input_tokens"], 8)
        self.assertEqual(partial["measurement_status"], "partial")
        complete = self.observe(
            [assistant, {"type": "result", "duration_ms": 600000, "usage": {"input_tokens": 10, "output_tokens": 5}}],
            "tclaude",
        )
        self.assertEqual(complete["usage"]["input_tokens"], 10)
        self.assertEqual(complete["measurement_status"], "complete")

    def test_bounded_scan_is_partial_and_incomplete_tail_is_ignored(self):
        path = self.root / "stdout.log"
        # Stay above one 64 KiB read chunk so the backward scan crosses chunks.
        with patch.object(model_usage.session_history, "PAGE_SCAN_BYTES", 2 * 65536):
            prefix = b"x\n" * (model_usage.session_history.PAGE_SCAN_BYTES // 2 + 1)
            event = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}).encode()
            path.write_bytes(prefix + event + b"\n" + event)
            observed = model_usage.read_log(self.root, str(path), agent="tcodex")
        self.assertEqual(observed["usage"]["input_tokens"], 10)
        self.assertEqual(observed["measurement_status"], "partial")

    def test_completed_previous_turn_does_not_hide_interrupted_next_turn(self):
        observed = self.observe(
            [{"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}, {"type": "turn.started"}]
        )
        self.assertEqual(observed["usage"]["input_tokens"], 10)
        self.assertEqual(observed["measurement_status"], "partial")

    def test_provider_completion_time_retained_separately_from_observation(self):
        observed = self.observe(
            [{"type": "result", "timestamp": "2026-11-24T00:10:00Z", "usage": {"input_tokens": 10}}], "tclaude"
        )
        self.assertEqual(observed["events"]["model_completed"]["at"], "2026-11-24T00:10:00Z")

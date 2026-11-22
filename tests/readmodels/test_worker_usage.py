import json
from tests.support.temp_workspace import TemporaryWorkspace


class WorkerUsageTests(TemporaryWorkspace):
    def test_usage_parser_extracts_cbc_stream_json_result(self):
        from robert_agent.adapters.processes import model_usage

        log = "\n".join(
            [
                "not json",
                json.dumps({"type": "message", "usage": {"input_tokens": 1}}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "model": "claude-sonnet-4.6",
                        "duration_ms": 1234,
                        "num_turns": 1,
                        "total_cost_usd": 0.0123,
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "cache_creation_input_tokens": 2,
                            "cache_read_input_tokens": 3,
                        },
                    },
                    sort_keys=True,
                ),
            ]
        )

        stdout = self.root / "legacy.stdout.log"
        stdout.write_text(log)
        parsed = model_usage.read_legacy_log(str(stdout))

        self.assertTrue(parsed["usage_available"], parsed)
        self.assertEqual(parsed["source"], "cbc_stream_json")
        self.assertEqual(parsed["usage"]["input_tokens"], 10)
        self.assertEqual(parsed["total_cost_usd"], 0.0123)
        self.assertEqual(parsed["duration_ms"], 1234)
        self.assertEqual(parsed["num_turns"], 1)

    def test_usage_parser_handles_missing_result_event(self):
        from robert_agent.adapters.processes import model_usage

        stdout = self.root / "legacy.stdout.log"
        stdout.write_text('{"type": "message"}\nnot json')
        self.assertEqual(model_usage.read_legacy_log(str(stdout)), {})

    def test_receipt_marks_unreported_cache_write_and_uncached_input_as_unknown(self):
        from robert_agent.readmodels import usage

        metadata = {
            "usage": {
                "usage_available": True,
                "source": "codex_json",
                "usage": {"input_tokens": 30, "cached_input_tokens": 8, "output_tokens": 9},
            }
        }
        attempt = {"attempt_id": "a1", "metadata_json": json.dumps(metadata), "lineage": "request"}
        counts = usage.summarize_receipt_usage(self.root, [(attempt, [])])
        self.assertEqual(counts["input_tokens"], 30)
        self.assertEqual(counts["output_tokens"], 9)
        self.assertEqual(counts["cached_input_tokens"], 8)
        self.assertIsNone(counts["cache_creation_input_tokens"])
        self.assertIsNone(counts["uncached_input_tokens"])
        self.assertEqual(counts["billing_token_run_count"], 0)

    def test_opencode_usage_adds_completed_steps_and_ignores_duplicate_parts(self):
        from robert_agent.adapters.processes import model_usage

        def event(part_id, input_tokens, cost):
            return json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "id": part_id,
                        "type": "step-finish",
                        "cost": cost,
                        "tokens": {
                            "input": input_tokens,
                            "output": 2,
                            "reasoning": 1,
                            "cache": {"read": 3, "write": 0},
                        },
                    },
                }
            )

        first = event("prt_first", 7, 0.012)
        stdout = self.root / "stdout.log"
        stdout.write_text(first + "\n" + first + "\n" + event("prt_second", 9, 0) + "\n")
        observed = model_usage.read_log(self.root, str(stdout), agent="opencode")
        self.assertEqual(observed["usage"]["input_tokens"], 16)
        self.assertEqual(observed["usage"]["output_tokens"], 6)
        self.assertEqual(observed["usage"]["cache_read_input_tokens"], 6)
        self.assertEqual(observed["usage"]["cache_creation_input_tokens"], 0)
        self.assertAlmostEqual(observed["total_cost_usd"], 0.012)

    def test_gateway_usage_is_numeric_and_keeps_explicit_zero(self):
        from robert_agent.adapters.processes import model_usage

        observed = model_usage.gateway_usage(
            {
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 0,
                    "authentication": "hidden",
                    "cache_read_input_tokens": -5,
                    "cache_creation_input_tokens": True,
                },
                "total_cost_usd": 0,
            }
        )
        self.assertEqual(
            observed,
            {
                "source": "gateway",
                "usage_available": True,
                "usage": {"input_tokens": 8, "output_tokens": 0},
                "total_cost_usd": 0,
            },
        )

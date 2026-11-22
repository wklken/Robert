import json
from tests.support.temp_workspace import TemporaryWorkspace


class WorkerUsageTests(TemporaryWorkspace):
    def test_usage_parser_extracts_cbc_stream_json_result(self):
        from robert_agent.readmodels import usage

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

        parsed = usage.parse_cbc_stream_json(log)

        self.assertTrue(parsed["usage_available"], parsed)
        self.assertEqual(parsed["source"], "cbc_stream_json")
        self.assertEqual(parsed["usage"]["input_tokens"], 10)
        self.assertEqual(parsed["total_cost_usd"], 0.0123)
        self.assertEqual(parsed["duration_ms"], 1234)
        self.assertEqual(parsed["num_turns"], 1)

    def test_usage_parser_handles_missing_result_event(self):
        from robert_agent.readmodels import usage

        parsed = usage.parse_cbc_stream_json('{"type": "message"}\nnot json')

        self.assertFalse(parsed["usage_available"], parsed)
        self.assertEqual(parsed["source"], "no_result_event")

    def test_receipt_marks_unreported_cache_write_and_uncached_input_as_unknown(self):
        from robert_agent.readmodels import usage

        metadata = {
            "usage": {
                "usage_available": True,
                "source": "codex_json",
                "usage": {"input_tokens": 30, "cached_input_tokens": 8, "output_tokens": 9},
            }
        }
        attempt = {"metadata_json": json.dumps(metadata), "lineage": "request"}
        counts = usage.summarize_receipt_usage(self.root, [(attempt, [])])
        self.assertEqual(counts["input_tokens"], 30)
        self.assertEqual(counts["output_tokens"], 9)
        self.assertEqual(counts["cached_input_tokens"], 8)
        self.assertIsNone(counts["cache_creation_input_tokens"])
        self.assertIsNone(counts["uncached_input_tokens"])
        self.assertEqual(counts["billing_token_run_count"], 0)

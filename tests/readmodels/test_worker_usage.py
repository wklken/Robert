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

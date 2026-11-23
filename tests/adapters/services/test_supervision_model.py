"""No-tool supervision transport accepts reasoning without publishing it."""

from copy import deepcopy
from http.client import BadStatusLine
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from robert_agent.adapters.services import supervision_model as model
from robert_agent.foundation import question_choices
from robert_agent.supervision import decisions


class SupervisionModelTests(unittest.TestCase):
    def setUp(self):
        self.bundle = {
            "bundle_id": "decision-fixture",
            "bundle_sha256": "a" * 64,
            "instructions": "Return the supplied decision contract.",
            "evidence": [{"id": "completion", "data": {"unfinished_goal": True}}],
            "allowed_actions": ["wait", "ask_user", "notify_user"],
            "candidates": [],
        }
        self.decision = {
            "bundle_id": self.bundle["bundle_id"],
            "bundle_sha256": self.bundle["bundle_sha256"],
            "action": "wait",
            "candidate_id": None,
            "reason": "等待当前任务完成。",
            "evidence_ids": ["completion"],
            "expected_progress": "下次检查应看到任务结果。",
            "recheck_seconds": 120,
            "message": "",
        }

    def response(self, result=None, blocks=None, stop_reason="end_turn"):
        return {
            "stop_reason": stop_reason,
            "content": blocks
            if blocks is not None
            else [{"type": "text", "text": json.dumps(result or self.decision, ensure_ascii=False)}],
        }

    def call(self, response, kind="decision"):
        with patch.object(model, "send_request", return_value=BytesIO(json.dumps(response).encode())):
            return model.call(self.bundle, kind, "http://127.0.0.1:12345", "fixture-model", 5)

    def test_thinking_and_redacted_thinking_are_ignored(self):
        for reasoning in (
            {"type": "thinking", "thinking": "private reasoning", "signature": "private signature"},
            {"type": "thinking", "signature": "private signature"},
            {"type": "redacted_thinking", "data": "private encrypted reasoning"},
        ):
            with self.subTest(kind=reasoning["type"]):
                response = self.response()
                response["content"].insert(0, reasoning)
                result = self.call(response)
                self.assertEqual(result, self.decision)
                self.assertEqual(decisions.validate(json.dumps(result), self.bundle), self.decision)

    def test_plain_text_and_split_text_are_accepted(self):
        text = json.dumps(self.decision)
        for blocks in (
            [{"type": "text", "text": text}],
            [{"type": "text", "text": text[:30]}, {"type": "text", "text": text[30:]}],
        ):
            with self.subTest(blocks=len(blocks)):
                self.assertEqual(self.call(self.response(blocks=blocks)), self.decision)

    def test_ask_user_choices_survive_the_model_transport(self):
        self.bundle[question_choices.REQUIRED_FLAG] = True
        decision = {
            **self.decision,
            "action": "ask_user",
            "message": "如何继续？",
            "choices": [
                {"id": "wait", "label": "等待", "tradeoff": "等待维护者处理当前结果。"},
                {"id": "review", "label": "检查", "tradeoff": "先检查当前结果再继续。"},
            ],
        }
        result = self.call(self.response(decision))
        self.assertEqual(result, decision)
        self.assertEqual(decisions.validate(json.dumps(result), self.bundle), decision)

    def test_legacy_ask_user_choices_remain_supported(self):
        decision = {**self.decision, "action": "ask_user", "message": "如何继续？"}
        for choices in (None, [], [{"id": "wait", "label": "等待"}]):
            with self.subTest(choices=choices):
                output = {**decision, **({"choices": choices} if choices is not None else {})}
                result = self.call(self.response(output))
                self.assertEqual(result, output)
                self.assertEqual(decisions.validate(json.dumps(result), self.bundle), output)

    def test_required_question_choices_are_validated(self):
        self.bundle[question_choices.REQUIRED_FLAG] = True
        decision = {**self.decision, "action": "ask_user", "message": "如何继续？"}
        choices = [
            {"id": "wait", "label": "等待", "tradeoff": "等待当前结果。"},
            {"id": "review", "label": "检查", "tradeoff": "检查当前结果。"},
        ]
        invalid = (
            {},
            {"choices": None},
            {"choices": []},
            {"choices": choices[:1]},
            {"choices": choices * 3},
            {"choices": [choices[0], {**choices[1], "id": "wait"}]},
            {"choices": [choices[0], {**choices[1], "label": "等待"}]},
            {"choices": [choices[0], {**choices[1], "tradeoff": ""}]},
            {"choices": [choices[0], {**choices[1], "command": "unexpected"}]},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.call(self.response({**decision, **changes}))

    def test_choices_and_unknown_fields_cannot_widen_other_decisions(self):
        for action in ("wait", "notify_user"):
            for field in ("choices", "unexpected"):
                with self.subTest(action=action, field=field), self.assertRaises(ValueError):
                    self.call(self.response({**self.decision, "action": action, "message": "等待处理。", field: []}))

    def test_diagnosis_can_include_thinking(self):
        diagnosis = {
            "bundle_id": self.bundle["bundle_id"],
            "bundle_sha256": self.bundle["bundle_sha256"],
            "diagnosis_class": "expected_wait",
            "evidence_ids": ["completion"],
            "recommended_action": "wait",
            "expected_progress": "等待任务结果。",
        }
        response = self.response(diagnosis)
        response["content"].insert(0, {"type": "thinking", "thinking": "private reasoning"})
        self.assertEqual(self.call(response, "diagnosis"), diagnosis)

    def test_tool_unknown_and_malformed_blocks_are_rejected(self):
        for block in (
            {"type": "tool_use", "id": "tool-fixture", "name": "shell", "input": {}},
            {"type": "server_tool_use", "name": "web_search", "input": {}},
            {"type": "image", "source": {}},
            {"type": "text", "text": None},
            {"type": "thinking", "thinking": None},
            {"type": "redacted_thinking", "data": None},
            {},
            "text",
        ):
            with self.subTest(block=block):
                response = self.response()
                response["content"].append(block)
                with self.assertRaises(ValueError):
                    self.call(response)

    def test_incomplete_responses_and_missing_text_are_rejected(self):
        for reason in ("tool_use", "max_tokens", "pause_turn", None):
            with self.subTest(stop_reason=reason), self.assertRaises(ValueError):
                self.call(self.response(stop_reason=reason))
        for blocks in ([], None, [{"type": "thinking", "thinking": "private reasoning"}]):
            with self.subTest(content=blocks), self.assertRaises(ValueError):
                self.call({"stop_reason": "end_turn", "content": blocks})

    def test_invalid_decisions_remain_rejected_after_thinking(self):
        invalid = (
            {"bundle_id": "wrong-bundle"},
            {"bundle_sha256": "b" * 64},
            {"evidence_ids": ["invented_evidence"]},
            {"evidence_ids": ["completion", "completion"]},
            {"evidence_ids": []},
            {"action": "finish_supervision"},
            {"action": "repair_ci", "candidate_id": "unavailable"},
            {"recheck_seconds": True},
            {"reason": ""},
            {"action": "ask_user", "message": ""},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.response({**self.decision, **changes})
                response["content"].insert(0, {"type": "thinking", "thinking": "private reasoning"})
                with self.assertRaises(ValueError):
                    self.call(response)

    def test_decision_text_uses_the_current_2000_character_contract(self):
        decision = {**self.decision, "reason": "等" * 1500}
        result = self.call(self.response(decision))
        self.assertEqual(decisions.validate(json.dumps(result), self.bundle), decision)

    def test_duplicate_fields_and_markdown_are_rejected(self):
        for text in ('{"action":"wait","action":"ask_user"}', "```json\n" + json.dumps(self.decision) + "\n```"):
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                self.call(self.response(blocks=[{"type": "text", "text": text}]))

    def test_request_exposes_no_tools(self):
        with patch.object(model, "send_request", return_value=BytesIO(json.dumps(self.response()).encode())) as send:
            model.call(self.bundle, "decision", "http://127.0.0.1:12345", "fixture-model", 5)
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["tool_choice"], {"type": "none"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["system"], self.bundle["instructions"])

    def test_candidate_decisions_retain_action_scope(self):
        self.bundle["candidates"] = [{"id": "candidate-fixture", "action": "repair_ci"}]
        decision = {**self.decision, "action": "repair_ci", "candidate_id": "candidate-fixture"}
        self.assertEqual(self.call(self.response(decision)), decision)
        for action in ("wait", "notify_user", "invented_action"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.call(self.response({**decision, "action": action}))

    def test_response_and_output_limits_remain_enforced(self):
        with patch.object(model, "send_request", return_value=BytesIO(b" " * (model.MAX_RESPONSE_BYTES + 1))):
            with self.assertRaisesRegex(ValueError, "transport limit"):
                model.call(self.bundle, "decision", "http://127.0.0.1:12345", "fixture-model", 5)
        oversized = {**self.decision, "reason": "等" * 2000, "expected_progress": "等" * 2000}
        with self.assertRaisesRegex(ValueError, "protocol limit"):
            self.call(self.response(oversized))

    def test_http_failure_excludes_gateway_body(self):
        error = HTTPError("http://127.0.0.1:12345", 502, "private failure", {}, BytesIO(b"private body"))
        with patch.object(model, "send_request", side_effect=error):
            with self.assertRaisesRegex(model.ModelError, "^local model gateway returned HTTP 502$"):
                model.call(self.bundle, "decision", "http://127.0.0.1:12345", "fixture-model", 5)

    def test_malformed_http_response_has_a_fixed_error(self):
        with patch.object(model, "send_request", side_effect=BadStatusLine("private gateway response")):
            with self.assertRaisesRegex(model.ModelError, "^local model gateway returned an invalid HTTP response$"):
                model.call(self.bundle, "decision", "http://127.0.0.1:12345", "fixture-model", 5)

    def test_rate_limit_retains_transport_outcome(self):
        for header, expected in ((None, None), ("120", 120), ("invalid", None)):
            with self.subTest(header=header):
                headers = {} if header is None else {"Retry-After": header}
                error = HTTPError("http://127.0.0.1:12345", 429, "rate limit", headers, BytesIO(b"private body"))
                with patch.object(model, "send_request", side_effect=error):
                    result = model.call(self.bundle, "decision", "http://127.0.0.1:12345", "fixture-model", 5)
                provider_error = {"code": "rate_limited"}
                if expected is not None:
                    provider_error["retry_after_seconds"] = expected
                self.assertEqual(result["error"], provider_error)
                self.assertEqual(decisions.validate(json.dumps(result), self.bundle), result)

    def test_non_loopback_endpoints_and_untrusted_metadata_are_rejected(self):
        for endpoint in ("https://127.0.0.1:12345", "http://localhost:12345", "http://127.0.0.1:12345/path"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                model.checked_endpoint(endpoint)
        with tempfile.TemporaryDirectory() as root:
            metadata = Path(root) / "metadata.json"
            metadata.write_text(json.dumps({"url": "http://127.0.0.1:12345"}))
            metadata.chmod(0o600)
            self.assertEqual(model.gateway_endpoint(metadata), "http://127.0.0.1:12345")
            metadata.chmod(0o622)
            with self.assertRaises(ValueError):
                model.gateway_endpoint(metadata)
            metadata.chmod(0o600)
            link = Path(root) / "link.json"
            link.symlink_to(metadata)
            with self.assertRaises(ValueError):
                model.gateway_endpoint(link)
            metadata.write_text(json.dumps({"url": 12345}))
            with self.assertRaises(ValueError):
                model.gateway_endpoint(metadata)

    def test_command_outputs_only_validated_text_and_safe_failure_details(self):
        response = self.response()
        response["content"].insert(0, {"type": "thinking", "thinking": "private reasoning"})
        responses = [response, deepcopy(response)]
        responses[1]["content"][1]["text"] = json.dumps({**self.decision, "evidence_ids": ["private invented id"]})

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps(responses.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as root, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            metadata = Path(root) / "metadata.json"
            metadata.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}"}))
            metadata.chmod(0o600)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                command = [
                    sys.executable,
                    "-B",
                    "-m",
                    "robert_agent.adapters.services.supervision_model",
                    "--kind",
                    "decision",
                    "--gateway-metadata",
                    str(metadata),
                    "--model",
                    "fixture-model",
                ]
                for returncode in (0, 1):
                    result = subprocess.run(
                        command,
                        input=json.dumps(self.bundle),
                        text=True,
                        capture_output=True,
                        timeout=10,
                        cwd=root,
                        env={**os.environ, "HOME": root},
                    )
                    self.assertEqual(result.returncode, returncode, result.stderr)
                    self.assertNotIn("private", result.stdout + result.stderr)
                    if returncode == 0:
                        self.assertEqual(json.loads(result.stdout), self.decision)
                        self.assertEqual(result.stderr, "")
                    else:
                        self.assertEqual(result.stdout, "")
                        self.assertIn("model output references unknown evidence", result.stderr)
            finally:
                server.shutdown()
                thread.join(timeout=2)

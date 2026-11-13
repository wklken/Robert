"""Decision command failures retain bounded diagnostics without blocking pipes."""

import json
import sys
import threading
import unittest

from tests.support import TEST_ROOT
from robert_agent.adapters.processes import model_command


class ModelCommandTests(unittest.TestCase):
    def run_command(self, source):
        return model_command.run(
            {"command": [sys.executable, "-c", source], "timeout_seconds": 3, "environment_allowlist": []},
            "{}",
            threading.Event(),
            lambda identity: True,
        )

    def test_nonzero_exit_retains_redacted_error_and_exit_code(self):
        result = self.run_command(
            "import sys; sys.stderr.write('gateway unavailable at /root/private/model'); sys.exit(7)"
        )
        self.assertEqual(result["status"], "exited")
        self.assertTrue(result["stopped"])
        self.assertEqual(result.get("exit_code"), 7)
        self.assertEqual(result.get("safe_error"), "gateway unavailable at <local-path>")

    def test_secret_error_is_withheld(self):
        result = self.run_command(
            "import sys; sys.stderr.write('api_key=' + 'opaque-' + 'credential-value'); sys.exit(1)"
        )
        self.assertEqual(result["status"], "exited")
        self.assertNotIn("opaque-credential-value", str(result))
        self.assertIn("withheld", result.get("safe_error", ""))

    def test_url_userinfo_is_withheld_from_real_command_result(self):
        cases = json.loads((TEST_ROOT / "fixtures" / "url_userinfo.json").read_text())
        for case in cases:
            with self.subTest(case=case["name"]):
                error = f"Connection failed: https://{case['userinfo']}@proxy.example.org/path"
                result = self.run_command(f"import sys; sys.stderr.write({error!r}); sys.exit(7)")
                self.assertEqual(result["status"], "exited")
                self.assertEqual(result["exit_code"], 7)
                self.assertTrue(result["stopped"])
                self.assertFalse(result["stderr_truncated"])
                self.assertNotIn(case["userinfo"], str(result))
                self.assertEqual(result["safe_error"], "Model stderr contained sensitive text; details withheld.")

    def test_large_stderr_is_drained_and_withheld_without_losing_stdout(self):
        result = self.run_command("import sys; sys.stderr.write('x' * 200000); print('{}'); sys.exit(2)")
        self.assertEqual(result["status"], "exited")
        self.assertTrue(result["stopped"])
        self.assertEqual(result["output"].strip(), b"{}")
        self.assertTrue(result.get("stderr_truncated"))
        self.assertLess(len(result.get("safe_error", "")), 2001)
        self.assertIn("withheld", result.get("safe_error", ""))

    def test_successful_command_preserves_protocol_output(self):
        result = self.run_command("import sys; sys.stderr.write('a warning'); print('{}')")
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["output"].strip(), b"{}")
        self.assertEqual(result.get("exit_code"), 0)

    def test_invalid_utf8_error_is_withheld(self):
        result = self.run_command("import sys; sys.stderr.buffer.write(bytes([255])); sys.exit(1)")
        self.assertEqual(result["status"], "exited")
        self.assertIn("withheld", result["safe_error"])

    def test_stdout_limit_and_timeout_still_stop_the_process(self):
        for source, expected in (
            ("print('x' * 9000)", "invalid_output"),
            ("import sys,time; sys.stderr.write('waiting'); sys.stderr.flush(); time.sleep(10)", "timed_out"),
        ):
            with self.subTest(expected=expected):
                result = self.run_command(source)
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["stopped"])
                self.assertEqual(result["output"], b"")

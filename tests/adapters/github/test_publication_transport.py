"""Publication command diagnostics preserve process results and redact evidence."""

from datetime import datetime
import errno
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest import mock

from robert_agent.adapters.github import publication_transport as transport_module


def trace(status=403, headers=None, body=None, request_body=None):
    """Shape verified using the installed gh against a local HTTP server."""
    return (
        "* Request at 2026-11-26 10:24:17.365640244 +0800 CST m=+0.040869489\n"
        "* Request to https://api.github.com/repos/example/repo/pulls\n"
        "> POST /repos/example/repo/pulls HTTP/1.1\n"
        "> Host: api.github.com\n"
        "> Authorization: token fixture-auth-value\n"
        "> Cookie: session=fixture-cookie-value\n\n"
        + (json.dumps(request_body, indent=2) + "\n\n" if request_body is not None else "")
        + f"< HTTP/2.0 {status} Response\n"
        + "< Content-Type: application/json\n"
        + "".join(f"< {key}: {value}\n" for key, value in (headers or {}).items())
        + "\n"
        + (json.dumps(body, indent=2) + "\n\n" if body is not None else "")
        + "* Request took 1.123649ms\n"
    )


class PublicationTransportTests(unittest.TestCase):
    def run_result(self, stderr, *, stdout="", returncode=1, command=None):
        command = command or ["gh", "pr", "create"]
        runner = mock.Mock(return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr))
        adapter = transport_module.PublicationTransport(runner)
        result = adapter(command)
        return adapter, result, runner

    def test_enables_api_trace_only_for_gh_and_preserves_command_and_environment(self):
        for command in (["gh", "pr", "list"], ["/usr/bin/gh", "pr", "create"], ["gh", "api", "user"]):
            with self.subTest(command=command), mock.patch.dict(os.environ, {"GH_TOKEN": "ambient-fixture"}):
                before = dict(os.environ)
                env = {"GH_TOKEN": "account-fixture", "GH_HOST": "github.example", "GH_DEBUG": "0"}
                runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="[]", stderr=""))
                adapter = transport_module.PublicationTransport(runner)
                adapter(command, env=env, text=True, capture_output=True, timeout=15, cwd="/workspace")
                args, kwargs = runner.call_args
                self.assertIs(args[0], command)
                self.assertEqual(kwargs["env"], {**before, **env, "GH_DEBUG": "api"})
                self.assertEqual(kwargs["timeout"], 15)
                self.assertEqual(kwargs["cwd"], "/workspace")
                self.assertEqual(env["GH_DEBUG"], "0")
                self.assertEqual(dict(os.environ), before)
                self.assertNotIn("account-fixture", json.dumps(adapter.diagnostics))

        env = {"GIT_SSH_COMMAND": "account-ssh"}
        adapter, _, runner = self.run_result("fatal: could not resolve host: git.example", command=["git", "push"])
        self.assertNotIn("env", runner.call_args.kwargs)
        adapter(["git", "fetch"], env=env)
        self.assertIs(runner.call_args.kwargs["env"], env)
        self.assertEqual(adapter.failure_facts()["error_class"], "network_failure")

    def test_strips_real_debug_trace_preserves_json_and_keeps_safe_response_evidence(self):
        body = {"message": "API rate limit exceeded", "token": "response-fixture", "nested": {"cookies": ["hidden"]}}
        stdout = json.dumps(body)
        stderr = (
            trace(
                headers={"Retry-After": "20", "X-Github-Request-Id": "request-123", "Set-Cookie": "secret-cookie"},
                body=body,
                request_body={"title": "Example", "password": "request-fixture"},
            )
            + "gh: API rate limit exceeded (HTTP 403)\n"
        )
        adapter, result, _ = self.run_result(stderr, stdout=stdout)
        self.assertEqual(result.stdout, stdout)
        self.assertEqual(result.stderr, "gh: API rate limit exceeded (HTTP 403)\n")
        facts = adapter.failure_facts()
        self.assertEqual(facts["error_class"], "rate_limited")
        self.assertEqual(facts["http_status"], 403)
        self.assertEqual(facts["response_headers"]["retry-after"], "20")
        self.assertEqual(facts["response_headers"]["x-github-request-id"], "request-123")
        self.assertNotIn("set-cookie", facts["response_headers"])
        payload = json.dumps(adapter.diagnostics)
        for value in (
            "fixture-auth-value",
            "fixture-cookie-value",
            "response-fixture",
            "request-fixture",
            "secret-cookie",
            "hidden",
        ):
            self.assertNotIn(value, payload)
        entry = adapter.diagnostics[-1]
        self.assertIn("POST /repos/example/repo/pulls", entry["request_excerpt"])
        self.assertIn("API rate limit exceeded", entry["response_excerpt"])
        self.assertIn("API rate limit exceeded", entry["stdout_excerpt"])
        self.assertEqual(entry["returncode"], 1)
        self.assertGreaterEqual(entry["elapsed_seconds"], 0)
        self.assertEqual(datetime.fromisoformat(entry["timestamp"]).utcoffset().total_seconds(), 0)

    def test_classifies_only_proven_http_or_cli_facts(self):
        cases = [
            (429, {}, {}, "gh: request failed", "rate_limited"),
            (403, {"Retry-After": "5"}, {}, "gh: Forbidden", "rate_limited"),
            (403, {"X-Ratelimit-Remaining": "0"}, {}, "gh: Forbidden", "rate_limited"),
            (403, {}, {"message": "You have exceeded a secondary rate limit."}, "gh: Forbidden", "rate_limited"),
            (403, {}, {"message": "API rate limit exceeded for 127.0.0.1."}, "gh: Forbidden", "rate_limited"),
            (401, {"Retry-After": "5"}, {}, "gh: connection reset by peer", "authentication_required"),
            (403, {}, {"message": "Resource not accessible by integration"}, "gh: EOF", "permission_required"),
            (
                403,
                {"X-Ratelimit-Remaining": "9"},
                {"title": "rate limit exceeded"},
                "gh: failed",
                "permission_required",
            ),
            (404, {}, {}, "gh: connection reset by peer", "permission_required"),
            (422, {}, {}, "gh: unexpected end of JSON input", "configuration_required"),
            (502, {}, {}, "gh: Bad Gateway", "network_failure"),
            (200, {}, {"message": "rate limit exceeded"}, "gh: failed", None),
        ]
        for status, headers, body, error, expected in cases:
            with self.subTest(status=status, headers=headers, body=body):
                adapter, _, _ = self.run_result(trace(status, headers, body) + error + "\n")
                self.assertEqual(adapter.failure_facts().get("error_class"), expected)
                self.assertEqual(adapter.failure_facts()["http_status"], status)

    def test_git_and_untraced_gh_failures_are_classified_without_debug_false_positives(self):
        cases = [
            ("fatal: unable to access URL: Could not resolve host: example.org", "network_failure"),
            ("fatal: connection reset by peer", "network_failure"),
            ("fatal: Failed to connect: Connection refused", "network_failure"),
            ("fatal: Operation timed out", "network_failure"),
            ("gh: unexpected EOF", "network_failure"),
            ("gh: unexpected end of JSON input", "network_failure"),
            ("fatal: TLS handshake timeout", "network_failure"),
            ("fatal: SSL certificate problem: unable to get local issuer certificate", "network_failure"),
            ("fatal: Authentication failed for 'https://github.com/example/repo'", "authentication_required"),
            ("git@github.com: Permission denied (publickey).", "permission_required"),
            ("remote: Permission to example/repo denied to robot.", "permission_required"),
            ("fatal: unable to access URL: The requested URL returned error: 403", "permission_required"),
            ("gh: Bad credentials (HTTP 401)", "authentication_required"),
            ("fatal: unable to access URL: The requested URL returned error: 503", "network_failure"),
            ("gh: API rate limit exceeded (HTTP 403)", "rate_limited"),
            ("fatal: failed to push some refs", None),
            ("gh: failed to create pull request", None),
            ("warning: template mentions connection reset by peer\nfatal: failed", None),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                adapter, _, _ = self.run_result(error + "\n", command=["git", "push"])
                self.assertEqual(adapter.failure_facts().get("error_class"), expected)
        adapter, result, _ = self.run_result(
            trace(200, body={"message": "connection reset by peer"}, request_body={"body": "rate limit exceeded"})
            + "warning: template mentions EOF\ngh: failed\n"
        )
        self.assertNotIn("error_class", adapter.failure_facts())
        self.assertEqual(result.stderr, "warning: template mentions EOF\ngh: failed\n")

    def test_last_http_response_controls_headers_and_success_clears_failure_facts(self):
        stderr = (
            trace(403, {"Retry-After": "10"}) + trace(401, {"X-Github-Request-Id": "last"}) + "gh: Bad credentials\n"
        )
        adapter, _, _ = self.run_result(stderr)
        self.assertEqual(adapter.failure_facts()["http_status"], 401)
        self.assertEqual(adapter.failure_facts()["error_class"], "authentication_required")
        self.assertNotIn("retry-after", adapter.failure_facts()["response_headers"])
        adapter.runner = mock.Mock(return_value=SimpleNamespace(stdout="[]", stderr=trace(200), returncode=0))
        adapter(["gh", "pr", "list"])
        self.assertEqual(adapter.failure_facts(), {})
        self.assertEqual(len(adapter.diagnostics), 2)

    def test_successful_gh_api_with_truncated_json_becomes_failure_without_changing_stdout(self):
        for stdout in ('{"id":123', '[{"id":123}', '{"token":"partial-value'):
            with self.subTest(stdout=stdout):
                adapter, result, _ = self.run_result(
                    trace(201),
                    stdout=stdout,
                    returncode=0,
                    command=["gh", "api", "repos/example/repo/issues/1/comments"],
                )
                self.assertEqual(result.stdout, stdout)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(adapter.failure_facts()["error_class"], "network_failure")
                self.assertEqual(adapter.failure_facts()["http_status"], 201)
                self.assertIn("JSON", result.stderr)
                self.assertEqual(adapter.diagnostics[-1]["returncode"], 0)

    def test_json_validation_preserves_empty_non_json_and_formatted_api_output(self):
        cases = [
            (["gh", "api", "endpoint"], "", trace(204)),
            (["gh", "api", "endpoint"], "   ", trace(204)),
            (["gh", "api", "endpoint"], '{"id":123}', trace(201)),
            (["gh", "api", "endpoint", "--paginate"], "[1]\n[2]\n", trace(200)),
            (["gh", "api", "endpoint", "--jq", ".id"], "123", trace(200)),
            (["gh", "api", "endpoint", "--template={{.title}}"], "[a title", trace(200)),
            (["gh", "api", "endpoint", "--include"], "HTTP/2.0 200\n\n{}", trace(200)),
            (["gh", "api", "logs"], "plain log output", ""),
            (["gh", "pr", "create"], "https://github.com/example/repo/pull/1", ""),
        ]
        for command, stdout, stderr in cases:
            with self.subTest(command=command, stdout=stdout):
                adapter, result, _ = self.run_result(stderr, stdout=stdout, returncode=0, command=command)
                self.assertEqual(result.stdout, stdout)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(adapter.failure_facts(), {})

    def test_trace_request_body_and_warning_cannot_supply_failure_facts(self):
        stderr = (
            trace(200, body={"message": "ok"}, request_body={"body": "HTTP 429 connection reset"})
            + "warning: HTTP 429 connection reset\ngh: failed\n"
        )
        adapter, _, _ = self.run_result(stderr)
        self.assertNotIn("error_class", adapter.failure_facts())

    def test_timeout_redacts_partial_stdout_on_the_same_exception(self):
        error = subprocess.TimeoutExpired(
            ["gh", "api", "endpoint"],
            1,
            output=b'{"token":"timeout-secret',
            stderr=b"gh: timeout\nAuthorization: Bearer timeout-auth",
        )
        adapter = transport_module.PublicationTransport(mock.Mock(side_effect=error))
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            adapter(["gh", "api", "endpoint"])
        self.assertIs(raised.exception, error)
        self.assertNotIn(b"timeout-secret", error.output)
        self.assertNotIn(b"timeout-auth", error.stderr)
        self.assertEqual(adapter.failure_facts()["error_class"], "network_failure")

    def test_preserves_explicit_streams_and_check_contract_for_malformed_json(self):
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout='{"id":', stderr=trace(201)))
        adapter = transport_module.PublicationTransport(runner)
        command = ["gh", "api", "endpoint"]
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            adapter(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertIs(runner.call_args.args[0], command)
        self.assertNotIn("capture_output", runner.call_args.kwargs)
        self.assertEqual(runner.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(runner.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(raised.exception.returncode, 1)
        self.assertEqual(adapter.failure_facts()["error_class"], "network_failure")

    def test_authentication_text_and_timeout_http_status_override_network_messages(self):
        adapter, _, _ = self.run_result(
            "fatal: Authentication failed: connection reset by peer\n", command=["git", "push"]
        )
        self.assertEqual(adapter.failure_facts()["error_class"], "authentication_required")
        error = subprocess.TimeoutExpired(["gh"], 1, stderr=(trace(403) + "gh: timeout\n").encode())
        adapter = transport_module.PublicationTransport(mock.Mock(side_effect=error))
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter(["gh", "api", "endpoint"])
        self.assertEqual(adapter.failure_facts()["error_class"], "permission_required")

    def test_redacts_argv_body_and_partial_output_before_truncation_and_public_errors(self):
        command = [
            "gh",
            "pr",
            "create",
            "--body",
            "private-body-value",
            "--title=private-title",
            "-H",
            "Authorization: abc",
            "-f",
            "token=field-fixture",
        ]
        partial = '{"message":"useful", "access_token":"' + "private-fragment" * 1000
        adapter, result, runner = self.run_result(
            "gh: creation failed\nAuthorization: Bearer public-fixture\n" + partial,
            stdout=partial,
            command=command,
        )
        self.assertIs(runner.call_args.args[0], command)
        self.assertEqual(result.stdout, partial)
        self.assertIn("gh: creation failed", result.stderr)
        for value in (
            "private-body-value",
            "private-title",
            "field-fixture",
            "public-fixture",
            "private-fragment",
            "Authorization: abc",
        ):
            self.assertNotIn(value, json.dumps(adapter.diagnostics))
        self.assertNotIn("public-fixture", result.stderr)
        self.assertNotIn("private-fragment", result.stderr)
        self.assertLessEqual(len(adapter.diagnostics[-1]["stdout_excerpt"]), 4096)

    def test_exception_diagnostics_rethrow_same_exception_and_never_store_exception_argv(self):
        exceptions = [
            (
                subprocess.TimeoutExpired(
                    ["gh", "--body", "exception-secret"],
                    2,
                    output=b'{"token":"partial-secret',
                    stderr=(trace(200) + "gh: timeout\n").encode(),
                ),
                "network_failure",
            ),
            (OSError(errno.ECONNRESET, "connection reset with password=os-secret"), "network_failure"),
            (PermissionError(errno.EACCES, "permission denied", "private-file"), "permission_required"),
            (FileNotFoundError(errno.ENOENT, "missing executable", "private-file"), None),
            (
                subprocess.CalledProcessError(
                    1, ["gh", "--body", "exception-secret"], "{}", trace(403) + "gh: Forbidden\n"
                ),
                "permission_required",
            ),
        ]
        for error, expected in exceptions:
            with self.subTest(error=type(error).__name__):
                adapter = transport_module.PublicationTransport(mock.Mock(side_effect=error))
                with self.assertRaises(type(error)) as raised:
                    adapter(["gh", "api", "user"])
                self.assertIs(raised.exception, error)
                self.assertEqual(adapter.failure_facts().get("error_class"), expected)
                self.assertEqual(adapter.diagnostics[-1]["exception_type"], type(error).__name__)
                for secret in ("exception-secret", "partial-secret", "os-secret", "private-file"):
                    self.assertNotIn(secret, json.dumps(adapter.diagnostics))
                if isinstance(error, subprocess.CalledProcessError):
                    self.assertEqual(error.stderr, "gh: Forbidden\n")

    def test_bounded_diagnostics_bytes_and_missing_streams(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess(["gh"], 1, b"{}", b"gh: EOF\n"))
        adapter = transport_module.PublicationTransport(runner)
        for _ in range(15):
            result = adapter(["gh", "api", "user"])
        self.assertEqual(result.stdout, b"{}")
        self.assertEqual(result.stderr, b"gh: EOF\n")
        self.assertEqual(len(adapter.diagnostics), 12)
        adapter.runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="[]"))
        result = adapter(["gh", "pr", "list"])
        self.assertEqual(result.stdout, "[]")
        self.assertEqual(adapter.failure_facts(), {})

    def test_safe_diagnostics_revalidates_allowlisted_fields_and_fails_closed(self):
        entry = {
            "command": ["gh", "pr", "create", "--body", "journal-body-secret"],
            "returncode": 1,
            "elapsed_seconds": 0.2,
            "timestamp": "2026-11-26T02:24:17+00:00",
            "http_status": 403,
            "response_headers": {
                "Retry-After": "10",
                "Authorization": "journal-auth-secret",
                "set-cookie": "journal-cookie-secret",
            },
            "request_excerpt": '> Authorization: Bearer journal-request-secret\n{"token": "journal-token-secret"}',
            "response_excerpt": '{"message":"Forbidden", "nested":{"password":"journal-password-secret","cookies":["journal-cookies-secret"]}}',
            "stdout_excerpt": '{"refreshToken":"journal-partial-secret',
            "stderr_excerpt": "gh: Forbidden\n",
            "error_class": "permission_required",
            "exception_type": "CalledProcessError",
            "env": {"GH_TOKEN": "journal-env-secret"},
            "unknown": "journal-unknown-secret",
        }
        before = json.dumps(entry)
        safe = transport_module.safe_diagnostics([entry] * 15)
        self.assertEqual(len(safe), 12)
        self.assertEqual(json.dumps(entry), before)
        payload = json.dumps(safe)
        self.assertNotIn("-secret", payload)
        self.assertNotIn('"env"', payload)
        self.assertNotIn('"unknown"', payload)
        self.assertEqual(safe[0]["response_headers"], {"retry-after": "10"})
        self.assertIn("Forbidden", safe[0]["response_excerpt"])
        self.assertEqual(transport_module.safe_diagnostics(safe), safe)
        for malformed in (None, "bad", {"stderr_excerpt": "bad"}, [None, "bad", 3, [entry], {}]):
            with self.subTest(malformed=malformed):
                self.assertEqual(transport_module.safe_diagnostics(malformed), [])
        self.assertEqual(transport_module.safe_diagnostics([{"command": {"token": "secret"}, "returncode": "1"}]), [])
        invalid = {
            **entry,
            "http_status": True,
            "elapsed_seconds": float("nan"),
            "error_class": "network_failure password=secret",
        }
        safe_invalid = transport_module.safe_diagnostics([invalid])[0]
        self.assertNotIn("http_status", safe_invalid)
        self.assertNotIn("elapsed_seconds", safe_invalid)
        self.assertNotIn("error_class", safe_invalid)
        for malformed_fields in (
            {"exception_type": []},
            {"error_class": {}},
            {"elapsed_seconds": 10**1000},
            {"command": ["gh", {}]},
            {"response_headers": {"date": ["raw"]}},
            {"timestamp": "9999-12-31T23:59:59-23:59"},
            {"response_excerpt": '{"token":"<redacted>"journal-tail-secret'},
        ):
            with self.subTest(malformed_fields=malformed_fields):
                safe = transport_module.safe_diagnostics([{**entry, **malformed_fields}])
                self.assertNotIn("-secret", json.dumps(safe))

    def test_real_local_fake_gh_process_captures_without_global_environment_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "gh"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "assert os.environ['GH_DEBUG'] == 'api'\n"
                "print(json.dumps({'argv': sys.argv[1:]}))\n"
                f"sys.stderr.write({(trace(429, {'Retry-After': '3'}, {'message': 'limited'}) + 'gh: limited (HTTP 429)')!r})\n"
                "sys.exit(1)\n"
            )
            executable.chmod(0o700)
            command = [str(executable), "pr", "list", "--json", "number"]
            before = dict(os.environ)
            adapter = transport_module.PublicationTransport(subprocess.run)
            result = adapter(command, text=True, timeout=5)
            self.assertEqual(json.loads(result.stdout)["argv"], command[1:])
            self.assertEqual(result.args, command)
            self.assertEqual(result.stderr, "gh: limited (HTTP 429)")
            self.assertEqual(adapter.failure_facts()["error_class"], "rate_limited")
            self.assertEqual(dict(os.environ), before)

    @unittest.skipUnless(shutil.which("gh"), "gh CLI unavailable")
    def test_installed_gh_real_debug_format_against_local_http_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Ratelimit-Remaining", "0")
                self.end_headers()
                self.wfile.write(b'{"message":"API rate limit exceeded","token":"http-fixture-secret"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = transport_module.PublicationTransport(subprocess.run)
            result = adapter(
                ["gh", "api", f"http://127.0.0.1:{server.server_port}/probe"],
                text=True,
                env={"GH_TOKEN": "fixture-only"},
                timeout=5,
            )
            self.assertEqual(json.loads(result.stdout)["token"], "http-fixture-secret")
            self.assertEqual(result.stderr.strip(), "gh: API rate limit exceeded (HTTP 403)")
            self.assertEqual(adapter.failure_facts()["http_status"], 403)
            self.assertEqual(adapter.failure_facts()["error_class"], "rate_limited")
            self.assertNotIn("http-fixture-secret", json.dumps(adapter.diagnostics))
            self.assertIn("API rate limit exceeded", adapter.diagnostics[-1]["response_excerpt"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()

import json
import unittest

from tests.support import TEST_ROOT


class RedactionTests(unittest.TestCase):
    def test_url_userinfo_is_blocked_without_returning_credentials(self):
        from robert_agent.security import redaction

        cases = json.loads((TEST_ROOT / "fixtures" / "url_userinfo.json").read_text())
        for case in cases:
            for scheme, host in (("https", "proxy.example.org:8443"), ("SOCKS5", "[2001:db8::1]:1080")):
                with self.subTest(case=case["name"], scheme=scheme):
                    text = f"Connection failed\n{scheme}://{case['userinfo']}@{host}/path"
                    result = redaction.redact_text(text)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "blocked_secret")
                    self.assertNotIn(case["userinfo"], str(result))
                    self.assertEqual(result["findings"], [{"rule": "url_userinfo", "line": 2, "column": 1}])
                    self.assertFalse(redaction.redact_and_truncate("x" * 2000 + text, 2000)["ok"])

    def test_short_credential_assignments_are_blocked_before_truncation(self):
        from robert_agent.security import redaction

        for name in ("password", "passwd", "api_key", "ACCESS_TOKEN"):
            for value in ("a", "ab", "abc", "abcd", "abcde", "!abc", "@z", "!", "短密钥", "none!", "]ab"):
                for assignment in (f"{name}={value}", f'{name}: "{value}"', f"{name}='{value}!'"):
                    with self.subTest(assignment=assignment):
                        result = redaction.redact_text(assignment, strict_assignments=True)
                        self.assertFalse(result["ok"])
                        self.assertNotIn(assignment, json.dumps(result))
                        self.assertFalse(
                            redaction.redact_and_truncate("x" * 2000 + " " + assignment, 2000, strict_assignments=True)[
                                "ok"
                            ]
                        )

    def test_strict_diagnostics_cannot_hide_credentials_after_environment_references(self):
        from robert_agent.security import redaction

        for text in (
            "token=$TOKEN|password=longsecret",
            "token=$TOKEN:password=!abc",
            "TOKEN=os.environ.example?password=longsecret",
            "token=$TOKEN/password=abcde",
        ):
            with self.subTest(text=text):
                self.assertFalse(redaction.redact_and_truncate(text, 2000, strict_assignments=True)["ok"])

    def test_strict_diagnostics_preserve_placeholders_and_environment_references(self):
        from robert_agent.security import redaction

        for text in (
            "max_tokens = 10",
            "if token == x:",
            "api_key=...",
            "password=None;",
            "password=<redacted>",
            "TOKEN = os.environ['TOKEN']",
            "token=$TOKEN",
            "redis://localhost:6379/0?password=secret&db=1",
            "example (password=secret)",
        ):
            with self.subTest(text=text):
                result = redaction.redact_and_truncate(text, 2000, strict_assignments=True)
                self.assertTrue(result["ok"], result)

    def test_source_annotations_are_not_credentials_in_general_text(self):
        from robert_agent.security import redaction

        for text in (
            "def verify(token: str) -> bool:",
            "password: str | None = None",
            "token: dict[str, str]",
        ):
            with self.subTest(text=text):
                self.assertTrue(redaction.redact_text(text)["ok"])
                self.assertEqual(redaction.redact_and_truncate(text, 2000)["text"], text)

    def test_urls_without_userinfo_preserve_paths_queries_and_fragments(self):
        from robert_agent.security import redaction

        for text in (
            "https://proxy.example.org:8443/path",
            "https://example.org/users/alice@example.org",
            "https://example.org?email=alice@example.org",
            "https://example.org#alice@example.org",
            "socks5://[2001:db8::1]:1080",
            "git@github.com:example/repo.git",
        ):
            with self.subTest(text=text):
                result = redaction.redact_text(text)
                self.assertTrue(result["ok"])
                self.assertEqual(result["text"], text)

    def test_synthetic_password_literals_are_normalized_without_changing_url_shape(
        self,
    ):
        from robert_agent.security import redaction

        for text, expected in (
            (
                "redis://localhost:6379/0?password=secret&db=1",
                "redis://localhost:6379/0?password=<redacted>&db=1",
            ),
            ('password="secret"', 'password="<redacted>"'),
            ("PASSWORD='SECRET'", "PASSWORD='<redacted>'"),
            ("example: `password=secret`", "example: `password=<redacted>`"),
        ):
            with self.subTest(text=text):
                result = redaction.redact_text(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["text"], expected)
                self.assertEqual(redaction.redact_text(expected)["text"], expected)

    def test_example_password_does_not_exempt_other_credentials_or_literal_suffixes(
        self,
    ):
        from robert_agent.security import redaction

        for text in (
            "password=secret!suffix",
            "password=secret-value",
            "api_key=secret",
            "示例：password=private-example-value",
            "```text\npassword=secret\nAuthorization: Bearer private-value\n```",
        ):
            with self.subTest(text=text):
                result = redaction.redact_text(text)
                self.assertFalse(result["ok"], result)

    def test_blocked_text_reports_rule_and_location_without_returning_the_value(self):
        from robert_agent.security import redaction

        result = redaction.redact_text("Example report\npassword=private-example-value")
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["findings"],
            [{"rule": "credential_assignment", "line": 2, "column": 1}],
        )
        self.assertNotIn("private-example-value", str(result))

    def test_token_configuration_and_comparisons_are_not_credentials(self):
        from robert_agent.security import redaction

        for text in (
            "max_tokens = 10",
            "if token == x:",
            "use Authorization: none",
            "api_key=...",
            "password = None",
            "TOKEN = os.environ['TOKEN']",
        ):
            with self.subTest(text=text):
                self.assertTrue(redaction.redact_text(text)["ok"])

    def test_redacted_header_values_are_allowed_but_suffixes_and_other_headers_are_blocked(
        self,
    ):
        from robert_agent.security import redaction

        self.assertTrue(redaction.redact_text("Authorization: <redacted>\nCookie: <redacted>")["ok"])
        self.assertTrue(redaction.redact_text("Authorization:  <redacted>  \r\nCookie:\t<redacted>\r\n")["ok"])
        for text in (
            "Authorization: <redacted>suffix",
            "Cookie: <redacted>suffix",
            "Authorization: <redacted>\nCookie: private-value",
        ):
            with self.subTest(text=text):
                self.assertFalse(redaction.redact_text(text)["ok"])

    def test_literal_password_api_key_and_jwt_are_blocked(self):
        import base64
        from robert_agent.security import redaction

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(b'{"sub":"test-user"}').decode().rstrip("=")
        for text in (
            "password = " + "hunter2",
            'api_key="' + "private-example-value" + '"',
            "ACCESS_TOKEN=" + "private-example-value",
            header + "." + payload + "." + "a" * 43,
        ):
            with self.subTest(kind=text.split("=")[0]):
                self.assertFalse(redaction.redact_text(text)["ok"])

    def test_custom_authorization_schemes_and_quoted_literals_are_blocked(self):
        from robert_agent.security import redaction

        for text in (
            "Authorization: Negotiate " + "abcdefghijklmnop",
            "Authorization: ApiKey " + "private-value",
            'TOKEN="' + "os.environ-is-a-literal-secret" + '"',
            'PASSWORD="' + "$literal-private-value" + '"',
        ):
            with self.subTest(text=text):
                self.assertFalse(redaction.redact_text(text)["ok"])

    def test_secret_blocks_publication(self):
        from robert_agent.security import redaction

        result = redaction.redact_text("Authorization" + ": Bearer secret-token")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_secret")

    def test_local_path_and_internal_ip_are_replaced(self):
        from robert_agent.security import redaction

        result = redaction.redact_text(
            "failed in /Users/sample-user/workspace/project at 10.0.0.12 while running test_gateway"
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("/Users/sample-user", result["text"])
        self.assertNotIn("10.0.0.12", result["text"])
        self.assertIn("<local-path>", result["text"])
        self.assertIn("<internal-ip>", result["text"])
        self.assertIn("test_gateway", result["text"])

    def test_github_tokens_are_blocked(self):
        from robert_agent.security import redaction

        result = redaction.redact_text("token: " + "ghp_" + "1234567890abcdefghijk")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_secret")

    def test_common_cloud_and_service_tokens_are_blocked(self):
        from robert_agent.security import redaction

        samples = [
            "AWS_ACCESS_KEY_ID=" + "AKIA" + "1234567890ABCDEF",
            "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuv",
            "OPENAI_" + "API_KEY=" + "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890",
            "GOOGLE_" + "API_KEY=" + "AIza" + "SyA1234567890abcdefghijklmnopqr",
            "Bearer " + "abcdefghijklmnopqrstuvwxyz1234567890",
        ]

        for sample in samples:
            with self.subTest(sample=sample):
                result = redaction.redact_text(sample)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked_secret")

    def test_non_user_local_paths_and_internal_domains_are_replaced(self):
        from robert_agent.security import redaction

        result = redaction.redact_text(
            "failed in /root/workspace/app and /data/workspace/acme/app; see /tmp/robert.log on ci.example.internal"
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("/root/workspace", result["text"])
        self.assertNotIn("/data/workspace", result["text"])
        self.assertNotIn("/tmp/dd.log", result["text"])
        self.assertNotIn("ci.example.internal", result["text"])
        self.assertIn("<local-path>", result["text"])
        self.assertIn("<internal-domain>", result["text"])

    def test_home_path_is_replaced(self):
        from robert_agent.security import redaction

        result = redaction.redact_text("failed in /home/runner/work/project")

        self.assertTrue(result["ok"])
        self.assertNotIn("/home/runner", result["text"])
        self.assertIn("<local-path>", result["text"])

    def test_repository_relative_paths_with_local_path_segments_are_kept(self):
        from robert_agent.security import redaction

        text = (
            "see `src/dashboard/apigateway/apigateway/data/apigw-definitions/resources.yaml:1894`, "
            "app/tmp/cache.py, my-home/root/x and pkg.data/tmp/y"
        )

        result = redaction.redact_text(text)

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], text)

    def test_absolute_local_paths_after_punctuation_are_replaced(self):
        from robert_agent.security import redaction

        result = redaction.redact_text(
            'see `/data/workspace/app`, (/tmp/robert.log), file:///root/report.md and "/home/runner/x"'
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"].count("<local-path>"), 4)
        for leaked in (
            "/data/workspace",
            "/tmp/robert.log",
            "/root/report.md",
            "/home/runner",
        ):
            self.assertNotIn(leaked, result["text"])

    def test_redact_and_truncate_never_returns_unredacted_suffix(self):
        from robert_agent.security import redaction

        result = redaction.redact_and_truncate(
            "failed in /home/runner/work/project " + ("x" * 100),
            max_chars=40,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 40)
        self.assertIn("<local-path>", result["text"])
        self.assertNotIn("/home/runner", result["text"])

    def test_structured_and_common_secret_key_assignments_are_blocked(self):
        from robert_agent.security import redaction

        for key in ("apiKey", "secret_key", "private_key", "AWS_SECRET_ACCESS_KEY", "x-api-key", "accessToken"):
            for template in ('"{}": "private-test-value"', "{}=private-test-value"):
                with self.subTest(key=key, template=template):
                    self.assertFalse(redaction.redact_text(template.format(key))["ok"])
        for value in ('"cache_key": "normal-cache-key"', "monkey=banana", "max_tokens=100000"):
            self.assertTrue(redaction.redact_text(value)["ok"])


if __name__ == "__main__":
    unittest.main()


class CredentialScanBudgetTests(unittest.TestCase):
    def test_maximum_memory_text_does_not_rescan_every_hyphen(self):
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from robert_agent.security.redaction import redact_text; assert redact_text('a-' * 32768)['ok']",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

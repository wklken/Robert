import json
import unittest

from tests.support import TEST_ROOT


class RedactionTests(unittest.TestCase):
    def test_mask_patterns_are_consistent_for_headers_and_assignments(self):
        from robert_agent.security import redaction

        for placeholder in ("<redacted>", "[redacted]", "..."):
            for text in (
                f"Authorization: {placeholder}",
                f"Authorization: Bearer {placeholder}",
                f"Cookie: session={placeholder}",
                f"password={placeholder}",
                f'api_key="{placeholder}"',
            ):
                for strict in (False, True):
                    with self.subTest(text=text, strict=strict):
                        result = redaction.redact_text(text, strict_assignments=strict)
                        self.assertTrue(result["ok"], result)
                        self.assertEqual(result["text"], text)
            for text in (
                f"Authorization: Bearer {placeholder}actual-value",
                f"Authorization: {placeholder}\n  actual-value",
                f"password={placeholder}actual-value",
                f'api_key="{placeholder}actual-value"',
            ):
                for strict in (False, True):
                    with self.subTest(text=text, strict=strict):
                        self.assertFalse(redaction.redact_text(text, strict_assignments=strict)["ok"])

    def test_global_cookie_policy_accepts_complete_masked_values(self):
        from robert_agent.security import redaction

        for strict in (False, True):
            for text in (
                "Cookie: <redacted>",
                "Set-Cookie:\t<REDACTED>  \r\n",
                "Cookie: <redacted>\nFinding remains visible.",
                "Cookie: session=<redacted>",
                "Cookie: session=[redacted]; other=...",
                "Set-Cookie: session=<redacted>; Path=/; HttpOnly",
                "响应头为 Set-Cookie: session=<redacted>; Path=/，普通字段回显会话值。",
                "Cookie: <redacted>，普通字段回显会话值。",
                'Example: "Cookie: <redacted>"',
                "Cookie:\n  session=<redacted>",
            ):
                with self.subTest(text=text, strict=strict):
                    result = redaction.redact_text(text, strict_assignments=strict)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["text"], text)
            for text in (
                "Cookie: session=<redacted>suffix",
                "Cookie: session=[redacted]; other=actual-value",
                "Set-Cookie: session=<redacted>; Path=/; token=actual-value",
                "Cookie: <redacted>，普通字段回显会话值。\n  actual-value",
                "Cookie:\n  session=<redacted>;\n  other=actual-value",
            ):
                with self.subTest(text=text, strict=strict):
                    result = redaction.redact_text(text, strict_assignments=strict)
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["status"], "blocked_secret")
                    self.assertFalse(redaction.redact_and_truncate(text, 1, strict_assignments=strict)["ok"])

    def test_authorization_placeholder_never_exempts_folded_credentials(self):
        from robert_agent.security import redaction

        for ending in ("\n", "\r\n"):
            for header in (
                "Authorization: <redacted>",
                "Authorization: none",
                "Authorization: null",
                "Authorization: ...",
                "Authorization:",
            ):
                text = ending.join(("Finding before.", header, "  private-value", "Finding after.", ""))
                for strict in (False, True):
                    with self.subTest(header=header, ending=ending, strict=strict):
                        result = redaction.redact_text(text, strict_assignments=strict)
                        self.assertFalse(result["ok"], result)
                        self.assertNotIn("private-value", str(result))
                        self.assertFalse(redaction.redact_and_truncate(text, 1, strict_assignments=strict)["ok"])
                with self.subTest(header=header, ending=ending, policy="report"):
                    result = redaction.redact_report(text)
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(
                        result["text"],
                        ending.join(
                            (
                                "Finding before.",
                                "[redacted sensitive report line]",
                                "[redacted sensitive report line]",
                                "Finding after.",
                                "",
                            )
                        ),
                    )

    def test_report_and_global_cookie_masks_share_final_postscan(self):
        from robert_agent.security import redaction

        safe = "响应头为 `Set-Cookie: session=<redacted>; Path=/`，普通字段回显会话值。"
        text = safe + "\nAuthorization: Bearer private-value\nFinding: /home/sample/project at 10.0.0.12"
        result = redaction.redact_report(text)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["text"],
            safe + "\n[redacted sensitive report line]\nFinding: <local-path> at <internal-ip>",
        )
        self.assertTrue(redaction.redact_text(safe)["ok"])
        self.assertTrue(redaction.redact_text(result["text"], strict_assignments=True)["ok"])
        self.assertEqual(redaction.redact_report(result["text"]), result)

    def test_report_cookie_prose_never_exempts_credential_continuations(self):
        from robert_agent.security import redaction

        for header in (
            "Cookie: <redacted>，普通字段回显会话值。\n  真实密钥",
            "Set-Cookie: session=<redacted>; Path=/，普通字段回显会话值。\n  真实密钥",
            "Cookie: <redacted>，普通字段回显会话值。\n  actual-session",
        ):
            with self.subTest(header=header):
                text = "Finding before.\n" + header + "\nFinding after."
                self.assertFalse(redaction.redact_text(text)["ok"])
                result = redaction.redact_report(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(
                    result["text"],
                    "Finding before.\n[redacted sensitive report line]\n"
                    "[redacted sensitive report line]\nFinding after.",
                )

    def test_authorization_baseline_placeholders_and_multiline_real_values(self):
        from robert_agent.security import redaction

        for strict in (False, True):
            for text in (
                "Authorization: <redacted>",
                "Authorization: none",
                "Authorization: null",
                "Authorization: ...",
                "Authorization: <redacted>\nFinding remains visible.",
            ):
                with self.subTest(text=text, strict=strict):
                    self.assertEqual(redaction.redact_text(text, strict_assignments=strict)["text"], text)
            for text in (
                "Authorization:\n  ApiKey x",
                "Authorization:\nx",
                "Authorization:\r\n\tNegotiate private-value",
                "Authorization: Bearer\n  private-value",
                "Authorization: <redacted>suffix",
            ):
                with self.subTest(text=text, strict=strict):
                    result = redaction.redact_text(text, strict_assignments=strict)
                    self.assertFalse(result["ok"], result)
                    self.assertNotIn("private-value", str(result))
                    self.assertNotIn("text", result)

    def test_report_redacted_cookie_examples_are_safe_but_appended_values_still_block(self):
        from robert_agent.security import redaction

        for text in (
            "响应头为 Set-Cookie: session=<redacted>; Path=/，普通字段回显会话值。",
            "Cookie: session=<redacted>; other=<redacted>",
            "Set-Cookie: <redacted>",
            "Cookie: <redacted>",
        ):
            with self.subTest(text=text):
                result = redaction.redact_report(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["text"], text)
        for text in (
            "Cookie: session=actual-session",
            "Set-Cookie: session=actual-session; Path=/",
            "Cookie: <redacted>; session=actual-session",
            "Set-Cookie: session=<redacted>; other=actual-session",
        ):
            with self.subTest(text=text):
                self.assertFalse(redaction.redact_text(text)["ok"])

    def test_complete_report_redacted_cookie_headers_preserve_formatting(self):
        from robert_agent.security import redaction

        for text in (
            "Cookie: session=[redacted]; other=...",
            "Set-Cookie: session=<REDACTED>; Path=/; Secure; HttpOnly; SameSite=Lax",
            "响应头为 `Set-Cookie: session=<redacted>; Path=/`，普通字段回显会话值。",
            'Example: "Cookie: session=<redacted>; other=<redacted>"',
            "Cookie: session=\"<redacted>\"; other='[redacted]'",
            "Cookie:\n  session=<redacted>;\n  other=<redacted>\nFinding remains visible.",
            "Cookie: session=<redacted>;\nother=<redacted>",
            "Set-Cookie:\r\n\tsession=<redacted>;\r\n\tPath=/; HttpOnly\r\n",
            "```http\nCookie: session=<redacted>\nSet-Cookie: other=<redacted>; Path=/\n```",
        ):
            with self.subTest(text=text):
                result = redaction.redact_report(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["text"], text)
                self.assertTrue(redaction.redact_text(text)["ok"])

    def test_cookie_placeholders_do_not_exempt_appended_or_malformed_values(self):
        from robert_agent.security import redaction

        for text in (
            "Cookie: session=<redacted>actual-session",
            "Cookie: session=<redacted>; other=短密钥",
            "Cookie: session=<redacted>; other=x",
            "Cookie: session=<redacted>, other=actual-session",
            "Cookie: session=<redacted>，other=actual-session",
            "Cookie: session=<redacted>，会话=真实密钥",
            "Cookie: session=<redacted>，actual-session",
            "Cookie: session=<redacted>，会话值 actual-session",
            "Cookie: session=<redacted> actual-session",
            "Cookie: session=<redacted>普通字段",
            "Cookie: session=<redacted>; Path=/",
            "Set-Cookie: Path=/; session=<redacted>",
            "Set-Cookie: session=<redacted>; Path=/actual-session",
            "Set-Cookie: session=<redacted>; SameSite=actual-session",
            "Set-Cookie: session=<redacted>; Secure=actual-session",
            "Set-Cookie: session=<redacted>; Domain=actual-session",
            "Set-Cookie: session=<redacted>; actual-session",
            "Cookie: session=<redacted>;\n  other=actual-session",
            "Cookie: session=<redacted>;\nother=actual-session",
            "Cookie:\n  session=actual-session",
            "Cookie: <redacted>\n  session=actual-session",
            "Cookie: session=<redacted>\nCookie: other=actual-session",
            "Set-Cookie: session=<redacted>; Path=/\nSet-Cookie: other=actual-session",
            "Cookie: session=<redacted>`; other=actual-session",
            'Example: "Cookie: session=<redacted>"; other=actual-session',
            "Cookie:",
        ):
            with self.subTest(text=text):
                result = redaction.redact_text(text)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["status"], "blocked_secret")
                self.assertNotIn("actual-session", str(result))
                self.assertFalse(redaction.redact_and_truncate(text, 1)["ok"])
                report = redaction.redact_report(text)
                self.assertTrue(report["ok"], report)
                first, separator, rest = text.partition("\n")
                safe_first_header = bool(separator and rest.startswith(("Cookie:", "Set-Cookie:")))
                self.assertEqual(
                    report["text"],
                    "\n".join(
                        first if index == 0 and safe_first_header else "[redacted sensitive report line]"
                        for index, _ in enumerate(text.splitlines())
                    ),
                )

    def test_safe_cookie_headers_do_not_mask_other_secret_patterns(self):
        from robert_agent.security import redaction

        for secret in (
            "Authorization: Bearer private-value",
            "https://alice:private-value@example.org/path",
            '"apiKey": "private-value"',
            "ghp_" + "a" * 20,
        ):
            with self.subTest(kind=secret.split(":")[0]):
                text = "Set-Cookie: session=<redacted>; Path=/\n" + secret
                result = redaction.redact_text(text)
                self.assertFalse(result["ok"], result)
                self.assertNotIn(secret, str(result))

    def test_public_report_redaction_removes_credentials_and_preserves_findings(self):
        from robert_agent.security import redaction

        for ending in ("\n", "\r\n"):
            text = ending.join(
                (
                    "Finding: request handling skips validation.",
                    "Cookie: session=actual-session; other=<redacted>",
                    "Evidence: tests/security/test_redaction.py:12",
                    'Payload: {"apiKey": "private-value", "cache_key": "normal-value"}',
                    "Remedy: validate every cookie value.",
                    "password=x",
                    "Set-Cookie: session=<redacted>; Path=/",
                )
            )
            expected = ending.join(
                (
                    "Finding: request handling skips validation.",
                    "[redacted sensitive report line]",
                    "Evidence: tests/security/test_redaction.py:12",
                    "[redacted sensitive report line]",
                    "Remedy: validate every cookie value.",
                    "[redacted sensitive report line]",
                    "Set-Cookie: session=<redacted>; Path=/",
                )
            )
            with self.subTest(ending=ending):
                self.assertFalse(redaction.redact_text(text)["ok"])
                result = redaction.redact_report(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["text"], expected)
                for secret in ("actual-session", "private-value", "password=x"):
                    self.assertNotIn(secret, str(result))
                self.assertTrue(redaction.redact_text(result["text"], strict_assignments=True)["ok"])
                self.assertEqual(redaction.redact_report(text), result)
                self.assertEqual(redaction.redact_report(result["text"])["text"], expected)

    def test_public_report_redaction_removes_complete_wrapped_secret_spans(self):
        from robert_agent.security import redaction

        for secret in (
            "Cookie:\n  session=actual-session;\n  other=<redacted>",
            "Cookie:\nactual-session",
            "Cookie: session=<redacted>;\nother=actual-session",
            "Cookie: session=<redacted>;\n; other=actual-session",
            "Authorization: Bearer\n  private-value",
            "Authorization:\n  Bearer private-value",
            "password:\n  private-value",
            "-----BEGIN RSA PRIVATE KEY-----\nYWJjZGVm\nYWJjZGVm\n-----END RSA PRIVATE KEY-----",
        ):
            with self.subTest(kind=secret.split(":")[0]):
                text = "Finding before.\n" + secret + "\nFinding after.\n"
                self.assertFalse(redaction.redact_text(text)["ok"])
                result = redaction.redact_report(text)
                self.assertTrue(result["ok"], result)
                self.assertEqual(
                    result["text"],
                    "Finding before.\n"
                    + "[redacted sensitive report line]\n" * len(secret.splitlines())
                    + "Finding after.\n",
                )
                self.assertNotIn("actual-session", str(result))
                self.assertNotIn("YWJjZGVm", str(result))

        text = "Finding before.\n-----BEGIN PRIVATE KEY-----\nYWJjZGVm\ntruncated-key-body"
        result = redaction.redact_report(text)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["text"],
            "Finding before.\n[redacted sensitive report line]\n[redacted sensitive report line]\n"
            "[redacted sensitive report line]",
        )

    def test_public_report_redaction_applies_remaining_redaction_to_safe_copy(self):
        from robert_agent.security import redaction

        text = "Finding: /home/sample/project at 10.0.0.12\nAuthorization: Bearer private-value\npassword=secret"
        result = redaction.redact_report(text)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["text"],
            "Finding: <local-path> at <internal-ip>\n[redacted sensitive report line]\npassword=<redacted>",
        )
        self.assertNotIn("private-value", str(result))
        self.assertFalse(redaction.redact_text(text)["ok"])
        self.assertEqual(redaction.redact_report("Finding: no credentials.")["text"], "Finding: no credentials.")

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

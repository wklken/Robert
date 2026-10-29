import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class RedactionTests(unittest.TestCase):
    def test_token_configuration_and_comparisons_are_not_credentials(self):
        from robert_agent import redaction

        for text in ("max_tokens = 10", "if token == x:", "use Authorization: none",
                     "api_key=...", "password = None", "TOKEN = os.environ['TOKEN']"):
            with self.subTest(text=text):
                self.assertTrue(redaction.redact_text(text)["ok"])

    def test_literal_password_api_key_and_jwt_are_blocked(self):
        import base64
        from robert_agent import redaction

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip('=')
        payload = base64.urlsafe_b64encode(b'{"sub":"test-user"}').decode().rstrip('=')
        for text in ("password = " + "hunter2", 'api_key="' + "private-example-value" + '"',
                     "ACCESS_TOKEN=" + "private-example-value", header + '.' + payload + '.' + 'a' * 43):
            with self.subTest(kind=text.split('=')[0]):
                self.assertFalse(redaction.redact_text(text)["ok"])

    def test_custom_authorization_schemes_and_quoted_literals_are_blocked(self):
        from robert_agent import redaction

        for text in ("Authorization: Negotiate " + "abcdefghijklmnop",
                     "Authorization: ApiKey " + "private-value",
                     'TOKEN="' + 'os.environ-is-a-literal-secret' + '"',
                     'PASSWORD="' + '$literal-private-value' + '"'):
            with self.subTest(text=text):
                self.assertFalse(redaction.redact_text(text)["ok"])

    def test_secret_blocks_publication(self):
        from robert_agent import redaction

        result = redaction.redact_text(
            "Authorization" + ": Bearer secret-token"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_secret")

    def test_local_path_and_internal_ip_are_replaced(self):
        from robert_agent import redaction

        result = redaction.redact_text(
            "failed in /Users/sample-user/workspace/project "
            "at 10.0.0.12 while running test_gateway"
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("/Users/sample-user", result["text"])
        self.assertNotIn("10.0.0.12", result["text"])
        self.assertIn("<local-path>", result["text"])
        self.assertIn("<internal-ip>", result["text"])
        self.assertIn("test_gateway", result["text"])

    def test_github_tokens_are_blocked(self):
        from robert_agent import redaction

        result = redaction.redact_text(
            "token: " + "ghp_" + "1234567890abcdefghijk"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_secret")

    def test_common_cloud_and_service_tokens_are_blocked(self):
        from robert_agent import redaction

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
        from robert_agent import redaction

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
        from robert_agent import redaction

        result = redaction.redact_text("failed in /home/runner/work/project")

        self.assertTrue(result["ok"])
        self.assertNotIn("/home/runner", result["text"])
        self.assertIn("<local-path>", result["text"])

    def test_repository_relative_paths_with_local_path_segments_are_kept(self):
        from robert_agent import redaction

        text = (
            "see `src/dashboard/apigateway/apigateway/data/apigw-definitions/resources.yaml:1894`, "
            "app/tmp/cache.py, my-home/root/x and pkg.data/tmp/y"
        )

        result = redaction.redact_text(text)

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], text)

    def test_absolute_local_paths_after_punctuation_are_replaced(self):
        from robert_agent import redaction

        result = redaction.redact_text(
            "see `/data/workspace/app`, (/tmp/robert.log), file:///root/report.md and \"/home/runner/x\""
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"].count("<local-path>"), 4)
        for leaked in ("/data/workspace", "/tmp/robert.log", "/root/report.md", "/home/runner"):
            self.assertNotIn(leaked, result["text"])

    def test_redact_and_truncate_never_returns_unredacted_suffix(self):
        from robert_agent import redaction

        result = redaction.redact_and_truncate(
            "failed in /home/runner/work/project " + ("x" * 100),
            max_chars=40,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["text"]), 40)
        self.assertIn("<local-path>", result["text"])
        self.assertNotIn("/home/runner", result["text"])


if __name__ == "__main__":
    unittest.main()

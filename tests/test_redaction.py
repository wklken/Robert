import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class RedactionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class PublishDedupeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_publish_dedupe_acceptance_reuses_existing_comment_and_pr_markers(self):
        from robert_agent import publish_dedupe_acceptance
        result = publish_dedupe_acceptance.publish_dedupe_acceptance(
            workspace_dir=self.root / "publish-dedupe"
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["publish_result"]["published_count"], 2)
        self.assertEqual(result["publish_result"]["deduplicated_count"], 2)
        self.assertEqual(result["action_counts"], {"published": 2, "deduplicated": 2})
        commands = [call["command"] for call in result["gh_calls"]]
        self.assertEqual(commands[0][:3], ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"])
        self.assertEqual(commands[1][:3], ["gh", "pr", "list"])
        self.assertEqual(commands[2][:3], ["gh", "pr", "view"])
        self.assertFalse(any(command[:3] == ["gh", "api", "repos/x/y/issues/1/comments"] for command in commands))
        self.assertFalse(any(command[:3] == ["gh", "pr", "create"] for command in commands))


if __name__ == "__main__":
    unittest.main()

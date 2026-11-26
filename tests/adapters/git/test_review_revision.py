import subprocess
import tempfile
import unittest
from pathlib import Path

from robert_agent.adapters.git.review_revision import clean_head, git


class CleanHeadTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        git(self.repo, "init", "-b", "main")
        (self.repo / "source.py").write_text("value = 1\n")
        git(self.repo, "add", "source.py")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
        self.head = git(self.repo, "rev-parse", "HEAD")

    def test_clean_head_supports_attached_and_detached_checkouts(self):
        self.assertEqual(clean_head(self.repo, "main"), self.head)
        git(self.repo, "checkout", "--detach", self.head)
        self.assertEqual(clean_head(self.repo), self.head)
        with self.assertRaises(subprocess.CalledProcessError):
            clean_head(self.repo, "main")

    def test_branch_check_precedes_rejection_of_dirty_paths(self):
        (self.repo / "local.txt").write_text("working input\n")
        with self.assertRaisesRegex(ValueError, "assigned branch changed"):
            clean_head(self.repo, "other")
        with self.assertRaisesRegex(ValueError, "requires a clean worktree"):
            clean_head(self.repo, "main")

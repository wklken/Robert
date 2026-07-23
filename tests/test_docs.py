from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_readmes_contain_fixed_brand_copy_and_quick_start(self):
        for name in ["README.md", "README_ZH.md"]:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("Robert", text)
            self.assertIn("Your Repo Teammate", text)
            self.assertIn(
                "An AI teammate that takes care of your GitHub work.",
                text,
            )
            self.assertIn("pipx install robert-github-agent", text)
            self.assertIn("robert doctor", text)
            self.assertIn("robert service start", text)

    def test_governance_files_exist(self):
        for name in [
            "LICENSE",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "SUPPORT.md",
        ]:
            self.assertTrue((ROOT / name).is_file(), name)

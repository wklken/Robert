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
            self.assertIn("[English](README.md)", text)
            self.assertIn("[简体中文](README_ZH.md)", text)
            self.assertIn("```mermaid", text)
            self.assertIn(
                "https://github.com/wklken/Robert/blob/main/docs/agent-install.md",
                text,
            )

    def test_governance_files_exist(self):
        for name in [
            "LICENSE",
            "CHANGELOG.md",
            "COMMUNITY.md",
        ]:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_concepts_are_merged(self):
        concepts = ROOT / "docs" / "concepts.md"
        self.assertTrue(concepts.is_file())
        self.assertIn("```mermaid", concepts.read_text(encoding="utf-8"))
        self.assertFalse((ROOT / "docs" / "concepts").exists())

    def test_documentation_categories_are_merged(self):
        for name in ["development", "guides", "reference"]:
            with self.subTest(name=name):
                self.assertTrue((ROOT / "docs" / f"{name}.md").is_file())
                self.assertFalse((ROOT / "docs" / name).exists())

    def test_agent_install_guide_is_complete(self):
        text = (ROOT / "docs" / "agent-install.md").read_text(
            encoding="utf-8"
        )
        for fragment in [
            "pipx install robert-github-agent",
            "gh auth status",
            "robert init --non-interactive",
            "explicit confirmation before starting unattended operation",
            "no GitHub write occurred during setup",
        ]:
            self.assertIn(fragment, text)

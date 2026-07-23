from pathlib import Path
import tempfile
import unittest

from robert_agent.skills import discover_skill_names, route_skill_status


class SkillTests(unittest.TestCase):
    def test_required_and_recommended_skills_have_different_severity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "installed"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: installed-skill\ndescription: test\n---\n",
                encoding="utf-8",
            )
            installed = discover_skill_names([str(root)])
            result = route_skill_status(
                required=["missing-required"],
                recommended=["installed-skill", "missing-recommended"],
                installed=installed,
            )
        self.assertEqual(result["missing_required"], ["missing-required"])
        self.assertEqual(result["missing_recommended"], ["missing-recommended"])
        self.assertFalse(result["runnable"])

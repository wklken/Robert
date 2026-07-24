from pathlib import Path
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_project_metadata_matches_public_identifiers(self):
        data = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(data["project"]["name"], "robert-github-agent")
        self.assertEqual(
            data["project"]["scripts"]["robert"],
            "robert_agent.cli.main:main",
        )
        self.assertEqual(data["project"]["version"], "0.1.0b2")

    def test_ci_and_release_workflows_exist(self):
        self.assertTrue((ROOT / ".github/workflows/ci.yml").is_file())
        self.assertTrue((ROOT / ".github/workflows/release.yml").is_file())

    def test_runtime_dependency_allowlist_is_explicit(self):
        data = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            data["project"]["dependencies"],
            [
                "PyYAML>=6.0,<7",
                "supervisor>=4.2,<5",
            ],
        )

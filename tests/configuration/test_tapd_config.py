"""TAPD settings are opt-in and can only name configured repositories."""

import json
from pathlib import Path

import yaml

from robert_agent.configuration.validation import validate_config
from tests.support.board_control_integration import BoardControlIntegrationFixture


class TapdConfigTests(BoardControlIntegrationFixture):
    def configure(self, tapd):
        raw = json.loads(self.config_path.read_text())
        raw["tapd"] = tapd
        self.config_path.write_text(json.dumps(raw))
        return validate_config(self.config_path, skip_external=True)

    def project(self, **overrides):
        return {
            "host": "TAPD.example.com",
            "workspace_id": "20001234",
            "trusted_user": "alice",
            "accept_after": "2026-11-20T09:00:00+08:00",
            "label_rules": [{"label": "robert-repo", "repository": "example/repo"}],
            **overrides,
        }

    def test_disabled_by_default(self):
        result = validate_config(self.config_path, skip_external=True)
        self.assertEqual(
            result["tapd"], {"enabled": False, "executable": "tapd", "mention_name": "robert-wklken", "projects": []}
        )

    def test_valid_project_is_normalized(self):
        result = self.configure({"enabled": True, "projects": [self.project()]})
        self.assertTrue(result["ok"], result)
        project = result["tapd"]["projects"][0]
        self.assertEqual(project["host"], "tapd.example.com")
        self.assertEqual(project["accept_after"], "2026-11-20T01:00:00+00:00")

    def test_documented_example_validates_with_a_configured_repository(self):
        path = Path(__file__).resolve().parents[2] / "docs/features/tapd.md"
        example = yaml.safe_load(path.read_text().split("```yaml\n", 1)[1].split("```", 1)[0])["tapd"]
        for project in example["projects"]:
            for rule in project["label_rules"]:
                rule["repository"] = "example/repo"
        result = self.configure(example)
        self.assertTrue(result["ok"], result)

    def test_invalid_projects_fail_closed(self):
        cases = {
            "naive start": self.project(accept_after="2026-11-20T09:00:00"),
            "unknown repository": self.project(label_rules=[{"label": "a", "repository": "other/repo"}]),
            "duplicate label": self.project(
                label_rules=[
                    {"label": "a", "repository": "example/repo"},
                    {"label": "a", "repository": "example/repo"},
                ]
            ),
            "blank user": self.project(trusted_user=" "),
            "padded user": self.project(trusted_user=" alice"),
            "numeric workspace": self.project(workspace_id=20001234),
            "zero workspace": self.project(workspace_id="0"),
            "url host": self.project(host="https://tapd.example.com"),
            "no rules": self.project(label_rules=[]),
        }
        for name, project in cases.items():
            with self.subTest(name=name):
                self.assertFalse(self.configure({"enabled": True, "projects": [project]})["ok"])
        self.assertFalse(self.configure({"enabled": True, "projects": []})["ok"])
        self.assertFalse(self.configure({"enabled": True, "projects": [self.project(), self.project()]})["ok"])
        self.assertFalse(self.configure({"mention_name": "@robert"})["ok"])
        self.assertFalse(self.configure({"token": "secret"})["ok"])

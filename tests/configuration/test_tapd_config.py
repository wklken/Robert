"""Validation of disabled TAPD sources and their explicit scopes."""

from copy import deepcopy
from pathlib import Path

import yaml

from tests.support.config_v2 import ConfigV2Fixture


class TapdConfigTests(ConfigV2Fixture):
    def setUp(self):
        super().setUp()
        self.tapd = {
            "enabled": False,
            "host": "tapd.example",
            "executable": str(self.root / "not-installed-tapd"),
            "cwd": str(self.root),
            "api_timezone": "Asia/Shanghai",
            "bot_name": "robot-ops",
            "reply_name": "review.robot",
            "model_worker": "b",
            "projects": [
                {
                    "workspace_id": "001234",
                    "trusted_author": "operator.A",
                    "operators": ["desktop-user", "openclaw:telegram:personal:42"],
                    "repositories": ["example/test"],
                    "mentions": {"enabled": True, "activated_after": "2026-11-14T08:00:00+08:00"},
                    "rules": [
                        {
                            "id": "story-queue",
                            "assignee": "Someone.Else",
                            "label": "Route-Me",
                            "repo": "example/test",
                            "kinds": ["story", "bug"],
                            "repair": {"ci": True, "conflict": True},
                        }
                    ],
                },
            ],
        }
        self.config["repos"][0]["pr_automation"] = {
            "ci": {
                "check_allowlist": ["unit", "lint"],
                "max_wall_minutes": 31,
                "max_total_tokens": 500,
                "max_cost_usd": 1.5,
            },
            "conflict": {"max_wall_minutes": 14},
        }

    def validate_tapd(self, settings=None, **override):
        return self.validate({**self.config, "tapd": self.tapd if settings is None else settings, **override})

    def test_documented_disabled_example_validates(self):
        path = Path(__file__).resolve().parents[2] / "docs/features/tapd-source.md"
        settings = yaml.safe_load(path.read_text().split("```yaml\n", 1)[1].split("```", 1)[0])["tapd"]
        settings["model_worker"] = "b"
        settings["projects"][0]["repositories"] = ["example/test"]
        settings["projects"][0]["rules"][0]["repo"] = "example/test"
        result = self.validate_tapd(settings)
        self.assertTrue(result["ok"], result)

    def test_default_and_disabled_custom_identifiers_need_no_installed_cli(self):
        defaults = self.validate()["tapd"]
        self.assertFalse(defaults["enabled"])
        self.assertEqual((defaults["bot_name"], defaults["reply_name"]), ("robert-wklken", "robert-wklken"))
        result = self.validate_tapd()
        self.assertTrue(result["ok"], result)
        configured = result["tapd"]
        self.assertEqual((configured["bot_name"], configured["reply_name"]), ("robot-ops", "review.robot"))
        self.assertEqual(configured["host"], "tapd.example")
        self.assertEqual(configured["model_worker"], "b")
        project = configured["projects"][0]
        self.assertEqual(project["workspace_id"], "001234")
        self.assertEqual(project["trusted_author"], "operator.A")
        self.assertEqual(project["mentions"]["activated_after"], "2026-11-14T00:00:00+00:00")
        self.assertEqual(project["operators"], self.tapd["projects"][0]["operators"])
        rule = project["rules"][0]
        self.assertEqual((rule["assignee"], rule["label"]), ("Someone.Else", "Route-Me"))
        self.assertEqual(
            rule["repair"],
            {
                "max_attempts": 3,
                "ci": {
                    "enabled": True,
                    "check_allowlist": ["unit", "lint"],
                    "max_wall_minutes": 31,
                    "max_total_tokens": 500,
                    "max_cost_usd": 1.5,
                },
                "conflict": {"enabled": True, "max_wall_minutes": 14},
            },
        )
        self.assertFalse(result["repos"][0]["pr_automation"]["ci"]["enabled"])
        self.assertFalse(result["repos"][0]["pr_automation"]["conflict"]["enabled"])
        settings = deepcopy(self.tapd)
        del settings["reply_name"]
        self.assertEqual(self.validate_tapd(settings)["tapd"]["reply_name"], "robot-ops")

    def test_rule_repair_defaults_to_enabled_even_when_github_switches_are_off(self):
        settings = deepcopy(self.tapd)
        del settings["projects"][0]["rules"][0]["repair"]
        result = self.validate_tapd(settings)
        self.assertTrue(result["ok"], result)
        policy = result["tapd"]["projects"][0]["rules"][0]["repair"]
        self.assertTrue(policy["ci"]["enabled"])
        self.assertTrue(policy["conflict"]["enabled"])
        self.config["repos"][0]["pr_automation"]["ci"]["check_allowlist"] = []
        self.assertIn("ci.check_allowlist", self.validate_tapd(settings)["safe_error"])
        settings["projects"][0]["rules"] = []
        self.assertTrue(self.validate_tapd(settings)["ok"])

    def test_host_normalizes_before_matching_notification_scope(self):
        settings = {**self.tapd, "host": "PRIVATE-TAPD"}
        scope = {"host": "Private-Tapd", "workspace_id": "001234"}
        result = self.validate_tapd(settings, messaging={"tapd_projects": [scope]})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tapd"]["host"], "private-tapd")
        self.assertEqual(result["messaging"]["tapd_projects"], [{**scope, "host": "private-tapd"}])
        duplicate = self.validate_tapd(
            settings, messaging={"tapd_projects": [scope, {**scope, "host": "private-tapd"}]}
        )
        self.assertIn("distinct", duplicate["safe_error"])

    def test_enablement_cannot_override_unverified_capability(self):
        denied = self.validate_tapd({**self.tapd, "enabled": True})
        self.assertFalse(denied["ok"])
        self.assertIn("capability", denied["safe_error"])
        self.assertIn("author", denied["safe_error"])
        self.assertFalse(self.validate_tapd({**self.tapd, "author_verified": True})["ok"])

    def test_partial_or_contradictory_disabled_configs_are_rejected(self):
        cases = [
            (("host",), "https://tapd.example/path"),
            (("host",), "tapd.example:443"),
            (("executable",), "tapd"),
            (("cwd",), "relative-path"),
            (("api_timezone",), "not/a-timezone"),
            (("model_worker",), "missing"),
            (("bot_name",), "ｒｏｂｏｔ"),
            (("bot_name",), "robot\u200b"),
            (("reply_name",), "Robert\nResult"),
            (("projects", 0, "workspace_id"), 1234),
            (("projects", 0, "workspace_id"), "1234 "),
            (("projects", 0, "trusted_author"), " operator.A"),
            (("projects", 0, "operators"), ["operator", "operator"]),
            (("projects", 0, "operators"), ["openclaw:telegram:personal:42\u200b"]),
            (("projects", 0, "repositories"), ["missing/repo"]),
            (("projects", 0, "mentions", "activated_after"), "2026-11-14T08:00:00"),
            (("projects", 0, "rules", 0, "repo"), "missing/repo"),
            (("projects", 0, "rules", 0, "kinds"), ["task"]),
            (("projects", 0, "rules", 0, "assignee"), "Someone\nElse"),
            (("projects", 0, "rules", 0, "repair", "max_attempts"), 4),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                settings = deepcopy(self.tapd)
                cursor = settings
                for key in path[:-1]:
                    cursor = cursor[key]
                cursor[path[-1]] = value
                result = self.validate_tapd(settings)
                self.assertFalse(result["ok"], result)
                self.assertIn(path[-1], result["safe_error"])

    def test_duplicates_and_missing_effective_ci_allowlist_fail(self):
        duplicate = deepcopy(self.tapd)
        duplicate["projects"].append(deepcopy(duplicate["projects"][0]))
        self.assertIn("workspace_id", self.validate_tapd(duplicate)["safe_error"])
        duplicate["projects"].pop()
        duplicate["projects"][0]["rules"].append(deepcopy(duplicate["projects"][0]["rules"][0]))
        self.assertIn("duplicate", self.validate_tapd(duplicate)["safe_error"])
        self.config["repos"][0]["pr_automation"]["ci"]["check_allowlist"] = []
        self.assertIn("ci.check_allowlist", self.validate_tapd()["safe_error"])

    def test_project_only_messaging_has_no_implicit_repo_or_operator_grant(self):
        messaging = {
            "enabled": True,
            "provider": "openclaw",
            "channel": "telegram",
            "account": "personal",
            "target": "42",
            "conversation": "telegram:42",
            "allowed_senders": ["42"],
            "send_after": "2026-11-14T00:00:00Z",
            "tapd_projects": [{"host": "tapd.example", "workspace_id": "001234"}],
        }
        result = self.validate_tapd(messaging=messaging)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["messaging"].get("repos", []), [])
        self.assertEqual(result["messaging"]["tapd_projects"], messaging["tapd_projects"])
        self.assertFalse(self.validate_tapd(messaging={**messaging, "dispatch_enabled": True})["ok"])
        for scope in (
            {"host": "other.example", "workspace_id": "001234"},
            {"host": "tapd.example", "workspace_id": "1234"},
        ):
            with self.subTest(scope=scope):
                self.assertFalse(self.validate_tapd(messaging={**messaging, "tapd_projects": [scope]})["ok"])

    def test_tapd_credentials_are_not_forwardable_to_workers_or_models(self):
        for name in ("TAPD_ACCESS_TOKEN", "TAPD_API_USER", "TAPD_API_PASSWORD"):
            with self.subTest(name=name):
                override = deepcopy(self.config)
                override["workers"]["a"]["environment_allowlist"] = [f" {name} "]
                self.assertIn("environment_allowlist", self.validate(override)["safe_error"])
                override = deepcopy(self.config)
                override["supervision"] = {
                    "diagnosis": {"command": ["/usr/bin/false"], "environment_allowlist": [name]}
                }
                self.assertIn("prohibited", self.validate(override)["safe_error"])

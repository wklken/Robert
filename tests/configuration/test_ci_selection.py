from tests.support.config_v2 import ConfigV2Fixture


class CISelectionConfigTests(ConfigV2Fixture):
    def test_defaults_preserve_required_names(self):
        policy = self.validate()["repos"][0]["pr_automation"]["ci"]
        self.assertEqual(policy["check_mode"], "all")
        self.assertEqual(policy["workflow_path_allowlist"], [])

    def test_observed_mode_accepts_path_only_selection(self):
        self.config["repos"][0]["pr_automation"] = {
            "ci": {
                "enabled": True,
                "check_mode": "observed",
                "workflow_path_allowlist": [" dynamic/github-code-scanning/codeql "],
            }
        }
        result = self.validate()
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["repos"][0]["pr_automation"]["ci"]["workflow_path_allowlist"],
            ["dynamic/github-code-scanning/codeql"],
        )

    def test_selection_rejects_unknown_mode_empty_selection_and_duplicate_paths(self):
        for policy, field in (
            ({"enabled": True, "check_mode": "observed"}, "check_allowlist"),
            ({"check_mode": "any"}, "check_mode"),
            ({"workflow_path_allowlist": [" "]}, "workflow_path_allowlist"),
            (
                {"workflow_path_allowlist": [".github/workflows/ci.yml", " .github/workflows/ci.yml "]},
                "workflow_path_allowlist",
            ),
        ):
            with self.subTest(policy=policy):
                self.config["repos"][0]["pr_automation"] = {"ci": policy}
                result = self.validate()
                self.assertFalse(result["ok"], result)
                self.assertIn(field, result["safe_error"])

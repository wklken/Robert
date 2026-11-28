import unittest

from robert_agent.configuration.routes import ROUTE_POLICIES, resolve_route_config
from tests.support.config_v2 import ConfigV2Fixture


class PrePrReviewConfigTests(unittest.TestCase):
    def test_new_pr_review_is_per_repository_and_preserves_worker_order(self):
        config = {"default_worker": "writer", "workers": {"writer": {}, "reviewer": {}}}
        policy = ROUTE_POLICIES["new-pr"]
        self.assertNotIn("pre_pr_review", resolve_route_config(config, {}, policy))
        for names in (["reviewer"], ["reviewer", "writer"]):
            repo = {"routes": {"new-pr": {"pre_pr_review": {"workers": names}}}}
            self.assertEqual(resolve_route_config(config, repo, policy)["pre_pr_review"], {"workers": names})

    def test_only_repository_new_pr_can_enable_review(self):
        settings = {"pre_pr_review": {"workers": ["writer"]}}
        config = {"default_worker": "writer", "workers": {"writer": {}}}
        with self.assertRaisesRegex(ValueError, "repository"):
            resolve_route_config({**config, "routes": {"new-pr": settings}}, {}, ROUTE_POLICIES["new-pr"])
        with self.assertRaisesRegex(ValueError, "new-pr"):
            resolve_route_config(config, {"routes": {"review-pr": settings}}, ROUTE_POLICIES["review-pr"])


class DeploymentValidationConfigTests(ConfigV2Fixture):
    def setUp(self):
        super().setUp()
        self.skill_path = str(self.root / "skills" / "fast-local-k8s-context" / "SKILL.md")
        self.settings = {"worker": "a", "skill_path": self.skill_path}
        self.review = {"workers": ["b"], "deployment_validation": self.settings}
        self.config["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": self.review}}

    def test_opt_in_accepts_absolute_manifest_even_when_unavailable(self):
        result = self.validate()
        self.assertTrue(result["ok"], result)
        effective = resolve_route_config(result, result["repos"][0], ROUTE_POLICIES["new-pr"])
        self.assertEqual(effective["pre_pr_review"], self.review)

    def test_invalid_validation_worker_and_manifest_are_rejected(self):
        for field, value in (
            ("worker", "unknown"),
            ("skill_path", "fast-local-k8s-context/SKILL.md"),
            ("skill_path", str(self.root / "different.md")),
            ("skill_path", " " + self.skill_path),
            ("skill_path", "/tmp/invalid\npath/SKILL.md"),
        ):
            with self.subTest(field=field, value=value):
                self.review["deployment_validation"] = {**self.settings, field: value}
                result = self.validate()
                self.assertFalse(result["ok"], result)
                self.assertIn(field, result["safe_error"])

    def test_validation_requires_only_the_worker_and_skill_path(self):
        for settings in ({"worker": "a"}, {"skill_path": self.skill_path}, {**self.settings, "enabled": True}):
            with self.subTest(settings=settings):
                self.review["deployment_validation"] = settings
                result = self.validate()
                self.assertFalse(result["ok"], result)
                self.assertIn("deployment_validation", result["safe_error"])

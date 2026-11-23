import unittest

from robert_agent.configuration.routes import ROUTE_POLICIES, resolve_route_config


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

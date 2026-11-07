import copy
import json
import unittest


from robert_agent.resource_files import resource
from robert_agent.configuration.routes import (
    resolve_route_config,
    review_workers_for_author,
)
from tests.support.multi_review_config import ReviewConcurrencyConfigFixture


class MultiReviewConfigTests(unittest.TestCase):
    def setUp(self):
        self.policy = {"id": "review-pr", "worker": "codex"}
        self.config = {
            "default_worker": "codex",
            "workers": {"codex": {}, "claude": {}},
            "routes": {
                "review-pr": {
                    "multi_agent_review": {
                        "enabled": True,
                        "workers": ["codex", "claude"],
                        "author_allowlist": ["Alice"],
                    }
                }
            },
        }

    def test_only_pr_author_selects_multiple_workers(self):
        route = resolve_route_config(self.config, {}, self.policy)
        self.assertEqual(review_workers_for_author(route, "aLiCe"), ["codex", "claude"])
        self.assertEqual(review_workers_for_author(route, "bob"), ["codex"])
        self.assertEqual(review_workers_for_author(route, None), ["codex"])

    def test_disabled_or_empty_allowlist_preserves_first_worker(self):
        for field, value in [("enabled", False), ("author_allowlist", [])]:
            settings = dict(self.config["routes"]["review-pr"]["multi_agent_review"])
            settings[field] = value
            route = resolve_route_config(
                self.config,
                {"routes": {"review-pr": {"multi_agent_review": settings}}},
                self.policy,
            )
            self.assertEqual(review_workers_for_author(route, "alice"), ["codex"])

    def test_invalid_settings_are_rejected(self):
        for changes in [
            dict(enabled="yes"),
            dict(workers=["claude", "codex"]),
            dict(workers=["codex", "unknown"]),
            dict(workers=["codex", "codex"]),
            dict(author_allowlist="alice"),
            dict(extra=True),
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings = {
                    **self.config["routes"]["review-pr"]["multi_agent_review"],
                    **changes,
                }
                resolve_route_config(
                    self.config,
                    {"routes": {"review-pr": {"multi_agent_review": settings}}},
                    self.policy,
                )

    def test_cannot_enable_on_other_routes(self):
        self.config["routes"]["new-pr"] = self.config["routes"]["review-pr"]
        with self.assertRaises(ValueError):
            resolve_route_config(self.config, {}, {"id": "new-pr"})


class ReviewConcurrencyConfigTests(ReviewConcurrencyConfigFixture):
    def test_defaults_to_four_without_changing_task_concurrency(self):
        result = self.validate(self.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["model_max_concurrency"], 4)
        self.assertEqual(result["max_concurrency"], 1)
        self.assertEqual(result["repos"][0]["max_concurrency"], 1)

    def test_accepts_independent_global_review_limit(self):
        result = self.validate(
            {
                **self.config,
                "scheduler": {"max_concurrency": 1, "model_max_concurrency": 19},
            }
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["model_max_concurrency"], 19)
        self.assertEqual(result["max_concurrency"], 1)

    def test_review_limit_requires_a_positive_integer(self):
        for value in (True, False, 0, -1, 1.5, "2", None, [], {}):
            with self.subTest(value=value):
                result = self.validate({**self.config, "scheduler": {"model_max_concurrency": value}})
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["status"], "failed_config")
                self.assertIn("model_max_concurrency", result["safe_error"])

    def test_review_limit_cannot_be_overridden_by_repository_or_route(self):
        for location in ("repository", "global-route", "repository-route"):
            config = copy.deepcopy(self.config)
            if location == "repository":
                config["repos"][0]["model_max_concurrency"] = 2
            elif location == "global-route":
                config["routes"] = {"review-pr": {"model_max_concurrency": 2}}
            else:
                config["repos"][0]["routes"] = {"review-pr": {"model_max_concurrency": 2}}
            with self.subTest(location=location):
                result = self.validate(config)
                self.assertFalse(result["ok"], result)
                self.assertIn("model_max_concurrency", result["safe_error"])

    def test_schema_exposes_review_limit_only_at_root(self):
        schema = json.loads(resource("schemas/config.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["properties"]["scheduler"]["properties"]["model_max_concurrency"],
            {"type": "integer", "minimum": 1, "default": 4},
        )
        self.assertNotIn(
            "model_max_concurrency",
            schema["properties"]["repos"]["items"]["properties"],
        )
        self.assertNotIn("model_max_concurrency", schema["$defs"]["routeOverride"]["properties"])

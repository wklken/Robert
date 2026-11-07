import unittest


class VoteConfigLimitTests(unittest.TestCase):
    def test_only_nested_model_limit_is_accepted(self):
        from tests.support import multi_review_config as config_fixture

        fixture = config_fixture.ReviewConcurrencyConfigFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.validate(
            {**fixture.config, "scheduler": {"model_max_concurrency": 2}}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["model_max_concurrency"], 2)
        for field in ("model_max_concurrency", "review_max_concurrency"):
            self.assertFalse(fixture.validate({**fixture.config, field: 2})["ok"])

    def test_aliases_cannot_hide_an_invalid_boolean_legacy_limit(self):
        from tests.support import multi_review_config as config_fixture

        fixture = config_fixture.ReviewConcurrencyConfigFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.validate(
            {
                **fixture.config,
                "model_max_concurrency": 1,
                "review_max_concurrency": True,
            }
        )
        self.assertFalse(result["ok"], result)

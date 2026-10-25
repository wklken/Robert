import unittest

from robert_agent import discover, route, route_config


class VoteRoutingTests(unittest.TestCase):
    def test_explicit_vote_takes_priority_over_bug_background_and_pr_updates(self):
        body = '@robert-bot 投票：为修复失败选择方案 A 或 B'
        self.assertEqual(discover.infer_intent('An unrelated bug', body, 'comment'), 'vote')
        result = route.route_task({'intent': 'vote', 'source_type': 'pull_request',
                                  'has_open_dd_pr': True, 'mentions_dd': True})
        self.assertEqual(result['route_id'], 'vote')
        self.assertEqual(result['expected_output'], 'multi_agent_vote_report')
        self.assertEqual(result['allowed_github_actions'], ['comment'])

    def test_comparison_request_but_not_quoted_or_negated_vote(self):
        for text in ['@robert-bot 对比上面的 A、B 方案并给出推荐',
                     '@robert-bot compare A and B and vote for one',
                     '@robert-bot 让多个模型选择上面两条路线中更合适的一条']:
            self.assertEqual(discover.infer_intent('', text, 'comment'), 'vote')
        for text in ['@robert-bot 不要投票，先分析', '@robert-bot review the vote implementation',
                     '> @robert-bot 投票 A/B\n@robert-bot 解释这段代码',
                     '@robert-bot 如何实现多模型投票功能', '@robert-bot 实现投票功能',
                     '@robert-bot 不要比较方案，先分析']:
            self.assertNotEqual(discover.infer_intent('', text, 'comment'), 'vote')
        self.assertNotEqual(discover.infer_intent('投票 A/B', '@robert-bot explain this', 'comment'), 'vote')

    def test_vote_configuration_requires_multiple_known_workers_and_main_first(self):
        config = {'workers': {'one': {}, 'two': {}}, 'routes': {'vote': {
            'worker': 'one', 'multi_agent_vote': {'enabled': True, 'workers': ['one', 'two']}}}}
        policy = {'id': 'vote', 'expected_output': 'multi_agent_vote_report'}
        result = route_config.resolve_route_config(config, {}, policy)
        self.assertEqual(result['multi_agent_vote']['workers'], ['one', 'two'])
        for names in [['one'], ['one', 'one'], ['two', 'one'], ['one', 'missing']]:
            config['routes']['vote']['multi_agent_vote']['workers'] = names
            with self.assertRaises(ValueError):
                route_config.resolve_route_config(config, {}, policy)


class VoteConfigLimitTests(unittest.TestCase):
    def test_new_and_legacy_model_limit_aliases(self):
        from tests import test_multi_review_config as config_fixture
        fixture = config_fixture.ReviewConcurrencyConfigTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for update, expected in [({'model_max_concurrency': 2}, 2),
                                 ({'review_max_concurrency': 3}, 3),
                                 ({'review_max_concurrency': 2, 'model_max_concurrency': 2}, 2)]:
            result = fixture.validate({**fixture.config, **update})
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['model_max_concurrency'], expected)
        self.assertFalse(fixture.validate({**fixture.config, 'model_max_concurrency': 2, 'review_max_concurrency': 3})['ok'])

    def test_aliases_cannot_hide_an_invalid_boolean_legacy_limit(self):
        from tests import test_multi_review_config as config_fixture
        fixture = config_fixture.ReviewConcurrencyConfigTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.validate({**fixture.config, 'model_max_concurrency': 1, 'review_max_concurrency': True})
        self.assertFalse(result['ok'], result)

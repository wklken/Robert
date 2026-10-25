import unittest

from robert_agent.route_config import resolve_route_config, review_workers_for_author


class MultiReviewConfigTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'id': 'review-pr', 'worker': 'codex'}
        self.config = {'workers': {'codex': {}, 'claude': {}}, 'routes': {
            'review-pr': {'multi_agent_review': {
                'enabled': True, 'workers': ['codex', 'claude'],
                'author_allowlist': ['Alice']}}}}

    def test_only_pr_author_selects_multiple_workers(self):
        route = resolve_route_config(self.config, {}, self.policy)
        self.assertEqual(review_workers_for_author(route, 'aLiCe'), ['codex', 'claude'])
        self.assertEqual(review_workers_for_author(route, 'bob'), ['codex'])
        self.assertEqual(review_workers_for_author(route, None), ['codex'])

    def test_disabled_or_empty_allowlist_preserves_first_worker(self):
        for field, value in [('enabled', False), ('author_allowlist', [])]:
            settings = dict(self.config['routes']['review-pr']['multi_agent_review'])
            settings[field] = value
            route = resolve_route_config(self.config, {'routes': {'review-pr': {
                'multi_agent_review': settings}}}, self.policy)
            self.assertEqual(review_workers_for_author(route, 'alice'), ['codex'])

    def test_invalid_settings_are_rejected(self):
        for changes in [dict(enabled='yes'), dict(workers=['claude', 'codex']),
                        dict(workers=['codex', 'unknown']), dict(workers=['codex', 'codex']),
                        dict(author_allowlist='alice'), dict(extra=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                settings = {**self.config['routes']['review-pr']['multi_agent_review'], **changes}
                resolve_route_config(self.config, {'routes': {'review-pr': {
                    'multi_agent_review': settings}}}, self.policy)

    def test_cannot_enable_on_other_routes(self):
        self.config['routes']['new-pr'] = self.config['routes']['review-pr']
        with self.assertRaises(ValueError):
            resolve_route_config(self.config, {}, {'id': 'new-pr'})

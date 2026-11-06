import copy
from pathlib import Path
import tempfile
import unittest

import yaml

from robert_agent.validate_config import validate_config
from robert_agent.route_config import resolve_route_config


class ConfigV2Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'repo' / '.git').mkdir(parents=True)
        self.path = self.root / 'config.yml'
        self.config = {
            'version': 2, 'data_dir': str(self.root / 'data'), 'database': 'robert.sqlite3',
            'github': {'account': 'bot'}, 'default_worker': 'b',
            'workers': {
                'a': {'adapter': 'codex', 'command': 'codex', 'model': 'model-a', 'effort': 'high'},
                'b': {'adapter': 'claude', 'command': 'claude', 'model': 'model-b', 'effort': 'low'},
            },
            'repos': [{'full_name': 'example/test', 'checkout': str(self.root / 'repo'),
                       'worktrees': str(self.root / 'trees'), 'default_branch': 'main',
                       'trusted_actors': ['maintainer']}],
        }

    def validate(self, config=None):
        self.path.write_text(yaml.safe_dump(self.config if config is None else config, sort_keys=False))
        return validate_config(self.path, skip_external=True)

    def test_explicit_default_and_route_models(self):
        self.config['routes'] = {'new-pr': {'model': 'route-model'}}
        self.config['repos'][0]['routes'] = {'new-pr': {'effort': 'medium'}}
        result = self.validate()
        self.assertTrue(result['ok'], result)
        route = resolve_route_config(result, result['repos'][0], {'id': 'new-pr'})
        self.assertEqual(route['worker'], 'b')
        self.assertEqual(route['model'], 'route-model')
        self.assertEqual(route['effort'], 'medium')
        self.config['workers'] = dict(reversed(list(self.config['workers'].items())))
        self.assertEqual(self.validate()['default_worker']['name'], 'b')

    def test_rejects_old_formats_and_unknown_fields(self):
        mutations = [
            ('version', 1), ('version', None), ('max_concurency', 99),
            ('workflow_engine', {'mode': 'sqlite'}), ('review_max_concurrency', 2),
            ('route_worker_models', {}), ('routes', {'typo-route': {'worker': 'a'}}),
            ('github', {'account': 'bot', 'poll_seconds': 10}),
            ('scheduler', {'max_concurrency': True}),
            ('scheduler', {'max_concurrency': '3'}),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                result = self.validate({**self.config, field: value})
                self.assertFalse(result['ok'], result)
                self.assertIn(field, result['safe_error'])

    def test_duplicate_yaml_keys_fail(self):
        self.validate()
        self.path.write_text(self.path.read_text() + '\nversion: 2\n')
        result = validate_config(self.path, skip_external=True)
        self.assertFalse(result['ok'], result)
        self.assertIn('duplicate', result['safe_error'])

    def test_seconds_are_not_truncated(self):
        self.config['scheduler'] = {'lease_ttl_seconds': 61}
        self.config['supervision'] = {'hard_timeout_seconds': 121, 'heartbeat_stale_seconds': 31}
        result = self.validate()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['lease_ttl_seconds'], 61)
        self.assertEqual(result['hard_timeout_seconds'], 121)

    def test_inheritance_and_native_models_are_separate(self):
        self.config['routes'] = {'review-pr': {
            'worker': 'a', 'model': 'single-model',
            'multi_agent_review': {'enabled': True, 'workers': ['a', 'b'], 'author_allowlist': ['Alice']},
        }}
        result = self.validate()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['workers'][0]['default_model'], 'model-a')
        override = copy.deepcopy(self.config)
        override['repos'][0]['routes'] = {'review-pr': {'worker': 'b'}}
        self.assertFalse(self.validate(override)['ok'])

    def test_native_global_settings_can_depend_on_real_repository_worker_override(self):
        for route_id, field in (('review-pr', 'multi_agent_review'), ('vote', 'multi_agent_vote')):
            with self.subTest(route_id=route_id):
                self.config['routes'] = {route_id: {field: {'enabled': True, 'workers': ['a', 'b']}}}
                self.config['repos'][0]['routes'] = {route_id: {'worker': 'a'}}
                result = self.validate()
                self.assertTrue(result['ok'], result)
                effective = resolve_route_config(result, result['repos'][0], {'id': route_id})
                self.assertEqual(effective[field]['workers'][0], effective['worker'])
                # A repository override must not hide unknown global references.
                self.config['routes'][route_id][field]['workers'] = ['missing', 'b']
                self.config['repos'][0]['routes'][route_id][field] = {
                    'enabled': True, 'workers': ['a', 'b']}
                self.assertIn('missing', self.validate()['safe_error'])

    def test_worker_deadlines_coexist_with_continuous_supervision_settings(self):
        self.config['supervision'] = {
            'mode': 'recover_known', 'poll_seconds': 17, 'batch_size': 7, 'stall_seconds': 91,
            'heartbeat_stale_seconds': 31, 'hard_timeout_seconds': 121, 'startup_grace_seconds': 15,
            'diagnosis': {'command': ['/usr/bin/false'], 'timeout_seconds': 20},
            'recovery': {'allowed_actions': ['audit_retry'], 'repositories': ['example/test']},
            'repair': {'repository': 'example/test', 'mode': 'backlog'},
            'build_manifest': '/tmp/build.json',
        }
        result = self.validate()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['heartbeat_stale_seconds'], 31)
        self.assertEqual(result['hard_timeout_seconds'], 121)
        self.assertEqual(result['worker_startup_grace_seconds'], 15)
        settings = result['supervision']
        self.assertEqual((settings['mode'], settings['poll_seconds'], settings['batch_size'],
                          settings['stall_seconds']), ('recover_known', 17, 7, 91))
        self.assertEqual(settings['diagnosis']['timeout_seconds'], 20)
        self.assertEqual(settings['diagnosis']['cooldown_seconds'], 600)
        self.assertEqual(settings['recovery']['allowed_actions'], ['audit_retry'])
        self.assertEqual(settings['repair']['mode'], 'backlog')
        self.assertEqual(settings['repair']['allowed_actions'], [])
        self.assertEqual(settings['build_manifest'], '/tmp/build.json')
        self.config['supervision']['diagnosis']['environment_allowlist'] = ['GH_TOKEN']
        self.assertIn('prohibited', self.validate()['safe_error'])

    def test_continuous_supervision_defaults_and_nullable_profiles_are_preserved(self):
        from robert_agent.supervision_config import DEFAULTS
        self.assertEqual(self.validate()['supervision'], DEFAULTS)
        self.config['supervision'] = {'mode': 'off', 'diagnosis': None,
                                      'repair': None, 'recovery': None, 'build_manifest': None}
        self.assertEqual(self.validate()['supervision'], {**DEFAULTS, 'mode': 'off'})
        self.config['supervision']['diagnosis'] = {'command': ['/usr/bin/false'] * 33}
        self.assertIn('at most 32', self.validate()['safe_error'])

    def test_integer_schema_checks_do_not_coerce_to_float(self):
        from robert_agent.validate_config import _schema_value
        large = 10 ** 500
        self.assertEqual(_schema_value(large, {'type': 'integer'}, {}, 'value'), large)

    def test_runtime_defaults_and_repository_account_override(self):
        import sys
        result = self.validate()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['python_bin'], sys.executable)
        self.assertEqual(result['max_concurrency'], 3)
        self.assertEqual(result['model_max_concurrency'], 4)
        self.assertEqual(result['hard_timeout_seconds'], 2700)
        self.assertEqual(result['heartbeat_stale_seconds'], 1200)
        self.assertEqual(result['lease_ttl_seconds'], 540)
        self.assertEqual(result['worker_startup_grace_seconds'], 300)
        self.assertEqual(result['repos'][0]['github_account'], 'bot')
        self.assertEqual(result['maintenance']['retention'],
                         {'runs_days': 7, 'failed_runs_days': 30, 'daemon_events_days': 7})
        self.assertFalse(result['repos'][0]['pr_automation']['ci']['enabled'])
        self.config['repos'][0]['github_account'] = 'other-bot'
        self.assertEqual(self.validate()['repos'][0]['github_account'], 'other-bot')

    def test_invalid_values_at_all_boundaries(self):
        cases = [
            (('database',), ''), (('database',), '/tmp/file.db'), (('database',), '..'),
            (('data_dir',), str(self.path)), (('python_bin',), ''),
            (('default_worker',), 'missing'), (('workers',), []),
            (('workers', 'a', 'adapter'), 'missing_adapter'),
            (('workers', 'a', 'command'), ''), (('workers', 'a', 'model'), ''),
            (('workers', 'a', 'effort'), ''), (('workers', 'a', 'fallback_model'), ''),
            (('workers', 'a', 'prompt_transport'), 'stdin'),
            (('workers', 'a', 'environment_allowlist'), ['GH_TOKEN']),
            (('workers', 'a', 'timeout_seconds'), 0),
            (('scheduler', 'max_concurrency'), 0), (('scheduler', 'model_max_concurrency'), -1),
            (('scheduler', 'lease_ttl_seconds'), 0),
            (('supervision', 'hard_timeout_seconds'), 0),
            (('supervision', 'heartbeat_stale_seconds'), '20'),
            (('supervision', 'startup_grace_seconds'), False),
            (('daemon', 'enabled'), 'true'), (('daemon', 'control_timeouts', 'local_cycle_seconds'), 0),
            (('maintenance', 'batch_size'), 501),
            (('skills', 'unknown'), []),
            (('repos', 0, 'trusted_actors'), []), (('repos', 0, 'trusted_actors'), 'alice'),
            (('repos', 0, 'trusted_actors'), [1]), (('repos', 0, 'trusted_actors'), ['']),
            (('repos', 0, 'checkout'), str(self.root / 'missing')),
            (('repos', 0, 'max_concurrency'), 4),
            (('repos', 0, 'pr_automation', 'ci', 'enabled'), True),
            (('repos', 0, 'pr_automation', 'ci', 'max_cost_usd'), float('nan')),
            (('repos', 0, 'pr_automation', 'conflict', 'max_attempts'), True),
            (('repos', 0, 'pr_automation', 'ci', 'check_allowlist'), ['unit', 'unit']),
        ]
        for path, value in cases:
            config = copy.deepcopy(self.config)
            parent = config
            for key in path[:-1]:
                parent = parent[key] if isinstance(key, int) else parent.setdefault(key, {})
            parent[path[-1]] = value
            with self.subTest(path=path, value=value):
                result = self.validate(config)
                self.assertFalse(result['ok'], result)
                self.assertIn(str(path[-1]), result['safe_error'])

    def test_worker_credentials_are_rejected_after_whitespace_normalization(self):
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
            for value in (name, f' {name} '):
                with self.subTest(value=value):
                    self.config['workers']['a']['environment_allowlist'] = [value]
                    result = self.validate()
                    self.assertFalse(result['ok'], result)
                    self.assertIn('environment_allowlist', result['safe_error'])

    def test_duplicate_repos_and_missing_required_fields(self):
        self.config['repos'].append(copy.deepcopy(self.config['repos'][0]))
        self.assertIn('duplicate', self.validate()['safe_error'])
        self.config['repos'].pop()
        for path in [('github',), ('default_worker',), ('workers', 'a', 'command')]:
            config = copy.deepcopy(self.config)
            parent = config
            for key in path[:-1]:
                parent = parent[key]
            del parent[path[-1]]
            with self.subTest(path=path):
                self.assertIn(path[-1], self.validate(config)['safe_error'])

    def test_custom_worker_and_pr_automation_preserved(self):
        self.config['workers']['a'].update(adapter='command', command=['custom', '--batch'],
                                         fallback_model='fallback', timeout_seconds=91)
        self.config['repos'][0]['pr_automation'] = {
            'ci': {'enabled': True, 'check_allowlist': ['unit'], 'max_cost_usd': 2.5},
            'conflict': {'enabled': True, 'max_wall_minutes': 33},
        }
        result = self.validate()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['workers'][0]['command_argv'], ['custom', '--batch'])
        self.assertEqual(result['workers'][0]['fallback_model'], 'fallback')
        self.assertEqual(result['repos'][0]['pr_automation']['conflict']['max_wall_minutes'], 33)

    def test_config_show_exposes_effective_repository_routes(self):
        from contextlib import redirect_stdout
        import io
        import json
        from robert_agent.cli.main import main
        self.config['routes'] = {'new-pr': {'worker': 'a', 'model': 'route-model'}}
        self.config['repos'][0]['routes'] = {'new-pr': {'effort': 'medium'}}
        self.validate()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['config', 'show', '--config', str(self.path), '--output', 'json'])
        self.assertEqual(code, 0, output.getvalue())
        result = json.loads(output.getvalue())
        route = result['effective_routes']['example/test']['new-pr']
        self.assertEqual((route['worker'], route['model'], route['effort']),
                         ('a', 'route-model', 'medium'))
        self.assertIn('native multi-model', result['model_scope'])

    def test_seconds_reach_lease_and_supervision_boundaries(self):
        from contextlib import closing
        from datetime import datetime, timedelta, timezone
        import sqlite3
        from robert_agent import run_once, storage, supervise
        self.config['scheduler'] = {'lease_ttl_seconds': 61}
        self.config['supervision'] = {'heartbeat_stale_seconds': 31, 'hard_timeout_seconds': 121}
        config = self.validate()
        self.assertTrue(config['ok'], config)
        now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        storage.init_database(config['db_path'])
        with closing(sqlite3.connect(config['db_path'])) as conn, conn:
            lease = run_once._acquire_agent_lease(conn, 'repo', 'run', now.isoformat(),
                                                  config['lease_ttl_seconds'])
            expires = conn.execute('SELECT expires_at FROM leases WHERE lease_id=?',
                                   (lease['lease_id'],)).fetchone()[0]
        self.assertEqual(datetime.fromisoformat(expires) - now, timedelta(seconds=61))
        for elapsed, expected in ((30, 'running'), (31, 'stale'), (120, 'stale'), (121, 'failed_timeout')):
            with self.subTest(elapsed=elapsed):
                state = supervise.classify_attempt(
                    heartbeat_at=now, started_at=now, now=now + timedelta(seconds=elapsed),
                    heartbeat_stale_seconds=config['heartbeat_stale_seconds'],
                    global_timeout_seconds=config['hard_timeout_seconds'])
                self.assertEqual(state['status'], expected)


if __name__ == '__main__':
    unittest.main()

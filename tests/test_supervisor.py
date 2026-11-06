from contextlib import closing
import json
import subprocess
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import storage


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'state.sqlite3'
        storage.init_database(self.db)
        self.config = {'db_path': str(self.db), 'data_dir': str(self.root), 'repos': [],
                       'config_path': str(self.root / 'config.yml'),
                       'supervision': {'mode': 'observe', 'poll_seconds': 60, 'batch_size': 100, 'stall_seconds': 300}}

    def observation(self):
        return {'scope_type': 'controller', 'scope_id': 'local', 'rule_id': 'controller_or_notice_stalled',
                'classification': 'confirmed', 'severity': 'error', 'reason_code': 'controller_stalled',
                'evidence': {'failure_count': 3}, 'expected_progress': {'condition': 'successful_cycle'},
                'lineage_key': None}

    def test_check_is_read_only_and_run_records_deduplicated_incident(self):
        from robert_agent import supervisor
        findings = {'observations': [self.observation()], 'cursor': '', 'coverage_complete': True}
        with patch('robert_agent.supervision_observe.collect', return_value=findings):
            result = supervisor.check(self.config)
            self.assertTrue(result['ok'])
            with closing(sqlite3.connect(self.db)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM supervision_incidents').fetchone()[0], 0)
            supervisor.check(self.config, record=True)
            supervisor.check(self.config, record=True)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM supervision_incidents').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='supervision_incident'").fetchone()[0], 1)

    def test_unreadable_database_records_health_without_creating_database(self):
        from robert_agent import supervisor
        self.config['db_path'] = str(self.root / 'missing.sqlite3')
        result = supervisor.check(self.config, record=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'unobservable')
        self.assertFalse(Path(self.config['db_path']).exists())
        health = json.loads(supervisor.health_path(self.config).read_text())
        self.assertEqual(health['status'], 'unobservable')

    def test_lock_is_exclusive_and_released(self):
        from robert_agent import supervisor
        with supervisor.instance_lock(self.config) as first:
            self.assertTrue(first)
            with supervisor.instance_lock(self.config) as second:
                self.assertFalse(second)
        with supervisor.instance_lock(self.config) as third:
            self.assertTrue(third)

    def test_configuration_rejects_unbounded_or_unknown_settings(self):
        from robert_agent.supervision_config import normalize
        for raw in ({'poll_seconds': 0}, {'batch_size': 100000}, {'mode': 'magic'}, {'typo': 1}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize(raw)
        self.assertEqual(normalize(None)['mode'], 'observe')

    def test_database_outage_notice_is_durable_and_deduplicated(self):
        from robert_agent import supervisor
        self.config['db_path'] = str(self.root / 'missing.sqlite3')
        supervisor.check(self.config, record=True, now="2026-11-06T00:00:00+00:00")
        calls = []
        def sent(argv, **kwargs):
            calls.append(json.loads(kwargs['input']))
            return subprocess.CompletedProcess(argv, 0)
        with patch.dict('os.environ', {'ROBERT_NOTIFICATION_COMMAND': 'notify-fixture'}):
            supervisor.deliver_health(self.config['db_path'], supervisor.health_path(self.config), runner=sent)
            self.assertEqual(calls, [])
            supervisor.check(self.config, record=True, now='2026-11-06T00:00:59+00:00')
            supervisor.deliver_health(self.config['db_path'], supervisor.health_path(self.config), runner=sent)
            self.assertEqual(calls, [])
            supervisor.check(self.config, record=True, now='2026-11-06T00:01:00+00:00')
            supervisor.deliver_health(self.config['db_path'], supervisor.health_path(self.config), runner=sent)
            supervisor.deliver_health(self.config['db_path'], supervisor.health_path(self.config), runner=sent)
        self.assertEqual(len(calls), 1)
        self.assertIn('delivery_id', calls[0])
        self.assertFalse(Path(self.config['db_path']).exists())

    def test_brief_writer_collision_records_health_without_notice_and_resets_episode(self):
        from robert_agent import supervisor
        calls = []
        def sent(argv, **kwargs):
            calls.append(json.loads(kwargs['input']))
            return subprocess.CompletedProcess(argv, 0)
        with patch.dict('os.environ', {'ROBERT_NOTIFICATION_COMMAND': 'notify-fixture'}):
            for minute in (0, 2):
                with closing(sqlite3.connect(self.db)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    result = supervisor.check(self.config, record=True, now=f'2026-11-06T00:0{minute}:00+00:00')
                    self.assertFalse(result['ok'])
                    supervisor.deliver_health(self.db, supervisor.health_path(self.config), runner=sent)
                    self.assertEqual(calls, [])
                result = supervisor.check(self.config, record=True, now=f'2026-11-06T00:0{minute}:01+00:00')
                self.assertTrue(result['ok'])
                self.assertNotIn('first_failed_at', json.loads(supervisor.health_path(self.config).read_text()))
        self.assertEqual(calls, [])

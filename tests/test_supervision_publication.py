from contextlib import closing
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_reliability
from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import execution_store, publish, storage, supervision_actions, supervision_publication


class SupervisedPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = test_reliability.PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db, self.now = fixture.db, fixture.now
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE workstreams SET active_task_id='t'")
            conn.execute("""UPDATE tasks SET metadata_json=json_object(
                'supervision_lineage_key','lineage','trigger_work_item_event_id','operator-event')""")
            execution_store.ensure_subject(conn, 'task', 't', 'human_request', self.now.isoformat())
        self.config = {'repos': [{'full_name': 'x/y'}], 'supervision': {'mode': 'recover_known', 'recovery': {
            'repositories': ['x/y'], 'allowed_actions': ['publication_retry', 'publication_reconcile']}}}
        self.commands = []
        self.remote = ''
        self.error = 'connection reset by peer'

    def runner(self, command, **kwargs):
        self.commands.append(command[:2])
        if command[:2] == ['git', 'ls-remote']:
            return SimpleNamespace(returncode=0, stdout=self.remote, stderr='')
        if command[:2] == ['git', 'push']:
            return SimpleNamespace(returncode=int(bool(self.error)), stdout='', stderr=self.error)
        raise AssertionError(command)

    def cycle(self, seconds=0, config=None):
        self.now += timedelta(seconds=seconds)
        with mock.patch.object(publish, '_now', return_value=self.now.isoformat()):
            kwargs = {'config': config} if config is not None else {}
            return publish.publish_ready_actions(self.db, run_command=self.runner, **kwargs)

    def intent(self):
        with closing(sqlite3.connect(self.db)) as conn:
            status, raw = conn.execute('SELECT status,remote_evidence_json FROM publication_intents').fetchone()
        return status, json.loads(raw)

    def actions(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT action_kind,status,budget_cost FROM supervision_recovery_actions ORDER BY rowid').fetchall()

    def verify(self, seconds=60):
        self.now += timedelta(seconds=seconds)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return supervision_actions.verify(conn, observations=[], now=self.now.isoformat())

    def test_absence_reconciliation_never_writes_until_new_claim(self):
        self.cycle()
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        self.error = ''
        self.cycle(60, self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        status, evidence = self.intent()
        self.assertEqual(status, 'confirmed_absent')
        self.assertTrue(evidence['supervision_retry_required'])
        self.assertEqual(self.actions(), [('publication_reconcile', 'observing', 1)])
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        self.verify()
        self.cycle()  # Legacy invocation cannot bypass the durable retry fence.
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 2)
        self.assertEqual(self.intent()[0], 'published')
        self.verify()
        self.assertEqual(self.actions(), [('publication_reconcile', 'succeeded', 1), ('publication_retry', 'succeeded', 1)])

    def test_confirmed_remote_effect_repairs_receipt_without_duplicate_push(self):
        self.cycle()
        self.remote = 'b' * 40 + '\trefs/heads/codex/fix\n'
        self.cycle(60, self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        self.assertEqual(self.intent()[0], 'published')
        self.verify()
        self.assertEqual(self.actions(), [('publication_reconcile', 'succeeded', 1)])

    def test_recovery_config_revoked_after_claim_prevents_push(self):
        self.cycle()
        self.cycle(60, self.config)
        self.verify()
        self.error = ''
        original = supervision_publication.guard
        with mock.patch.object(supervision_publication, 'guard', side_effect=lambda db, action, state, config, now:
                original(db, action, state, {}, now)):
            self.cycle(config=self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        self.assertEqual(self.intent()[0], 'confirmed_absent')
        self.assertEqual(self.actions()[-1], ('publication_retry', 'rejected', 0))

    def test_crashed_claim_reconciles_same_action_and_retains_write_budget(self):
        self.cycle()
        self.cycle(60, self.config)
        self.verify()
        original = self.runner

        def crash(command, **kwargs):
            if command[:2] == ['git', 'push']:
                self.commands.append(command[:2])
                raise RuntimeError('publisher crash during exact action')
            return original(command, **kwargs)

        with mock.patch.object(self, 'runner', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'publisher crash'):
                self.cycle(config=self.config)
        self.assertEqual(self.actions()[-1], ('publication_retry', 'applying', 1))
        self.cycle(60, self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 2)
        self.assertEqual(self.intent()[0], 'confirmed_absent')
        self.assertEqual(self.actions()[-1], ('publication_retry', 'failed', 1))
        self.error = ''
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 2)

    def test_mutation_only_policy_cannot_bypass_claim_via_legacy_reconciliation(self):
        self.cycle()
        self.error = ''
        self.config['supervision']['recovery']['allowed_actions'] = ['publication_retry']
        self.cycle(60, self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 1)
        self.assertTrue(self.intent()[1]['supervision_retry_required'])
        self.cycle(config=self.config)
        self.assertEqual(self.commands.count(['git', 'push']), 2)
        self.assertEqual(self.actions(), [('publication_retry', 'observing', 1)])

"""Explicit cutover recovery preserves the old native round and its result history."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest import mock

from tests import test_review_coordinator as fixtures
from robert_agent import board, discover, native_rounds, result_cutover, work_items


class NativeCutoverOperatorTests(unittest.TestCase):
    def test_stopped_review_retrigger_creates_new_round_preserving_original(self):
        fixture = fixtures.ReviewCoordinatorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cycle([fixture.event()])
        task, _ = fixture.current()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='running',metadata_json=json_set(metadata_json,'$.result_contract','old','$.dispatch',json(?))",
                (json.dumps({'ok': True, 'pid': 1073741824, 'process_exited': True, 'returncode': 0}),))
        self.assertTrue(result_cutover.prepare(fixture.db)['ok'])
        with closing(sqlite3.connect(fixture.db)) as conn:
            original = native_rounds.session(conn, task_id=task)
            item_id = conn.execute('SELECT work_item_id FROM work_items').fetchone()[0]
        self.assertIn('retrigger', board.get_work_item_detail(fixture.db, item_id)['valid_commands'])
        context = work_items.CommandContext('operator', 'owner', frozenset({'repo:example/backend'}), frozenset({'codex', 'claude'}))
        with mock.patch.object(discover, '_source_metadata', side_effect=lambda event, *args: ({**event, 'body': '@robert-bot review this PR'}, 'open')):
            requested = work_items.retrigger_failed_github_task(fixture.db, task, context=context,
                idempotency_key='native-recover', notifier=lambda _: None)
        self.assertTrue(requested['ok'], requested)
        fixture.cycle([])
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(native_rounds.session(conn, task_id=task), original)
            child = conn.execute('SELECT task_id FROM tasks WHERE parent_task_id=?', (task,)).fetchone()[0]
            recovered = native_rounds.session(conn, task_id=child)
            self.assertIsNotNone(recovered)
            self.assertNotEqual(recovered['round_id'], original['round_id'])
            self.assertEqual(recovered['request']['event']['event_fingerprint'], original['request']['event']['event_fingerprint'])

    def test_recovered_vote_clarification_keeps_selected_round_through_completion(self):
        from tests import test_vote_coordinator
        from robert_agent import vote_store
        fixture = test_vote_coordinator.VoteCoordinatorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture.cycle([fixture.event()])
        task, _ = fixture.fixture.current()
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='running',metadata_json=json_set(metadata_json,'$.result_contract','old','$.dispatch',json(?))",
                (json.dumps({'ok': True, 'pid': 1073741824, 'process_exited': True, 'returncode': 0}),))
        self.assertTrue(result_cutover.prepare(fixture.db)['ok'])
        with closing(sqlite3.connect(fixture.db)) as conn:
            original = vote_store.session(conn, task_id=task)
        context = work_items.CommandContext('operator', 'owner', frozenset({'repo:example/backend'}), frozenset({'codex', 'claude'}))
        with mock.patch.object(discover, '_source_metadata', side_effect=lambda event, *args: (event, 'open')):
            work_items.retrigger_failed_github_task(fixture.db, task, context=context,
                idempotency_key='recover-vote', notifier=lambda _: None)
        fixture.fixture.cycle([])
        fixture.clarify = True
        recovering = fixture.driver()
        self.assertNotEqual(recovering.round_id, original['round_id'])
        self.assertIsNone(recovering.run())
        fixture.fixture.cycle([])
        fixture.publish_all()
        fixture.fixture.cycle([], skip_publish=False)
        fixture.clarify = False
        fixture.fixture.cycle([{**fixture.event('clarification'), 'body': '@robert-bot latency target is 1 second', 'intent': 'analysis'}])
        resumed = fixture.driver()
        self.assertEqual(resumed.round_id, recovering.round_id)
        self.assertEqual(len(resumed.request['event']['clarifications']), 1)
        resumed.run()
        fixture.fixture.cycle([])
        fixture.publish_all()
        fixture.fixture.cycle([], skip_publish=False)
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(vote_store.session(conn, task_id=task), original)
            self.assertEqual(vote_store.session(conn, round_id=recovering.round_id)['status'], 'published')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM vote_sessions').fetchone()[0], 2)

from contextlib import closing
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from tests.support import github_board_recovery
from tests.support import review_coordinator
from tests.support import repository_cycle as test_run_once
from robert_agent.adapters.sqlite import review_rounds as review_store
from robert_agent.runtime import cycle as run_once
from robert_agent.supervision import lineage as supervision_lineage
from robert_agent.work.application import commands as work_items
from robert_agent.execution import ownership as execution_ownership
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import ready_work
from robert_agent.foundation import values


def _receipt(conn, scope_type, event_id):
    row = conn.execute('''SELECT metadata_json FROM supervision_checkpoints
        WHERE scope_type=? AND scope_id=? AND rule_id='admission' ''', (scope_type, event_id)).fetchone()
    return json.loads(row[0]) if row else None


def _task_lineage(conn, task_id):
    return conn.execute("SELECT json_extract(metadata_json,'$.supervision_lineage_key') FROM tasks WHERE task_id=?",
                        (task_id,)).fetchone()[0]


class SupervisionLineageTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def cycle(self, fixture):
        return run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path,
                                 dry_run=True, skip_external=True, skip_publish=True)

    def test_github_receipt_survives_preparation_failure_and_replay_admits_once(self):
        fixture = self.fixture(test_run_once.RepositoryCycleFixture)
        db = fixture.data_dir / 'dd.sqlite3'
        event_id = 'event:comment:comment-1'
        observed = []

        def fail_after_commit(*_args):
            with closing(sqlite3.connect(db)) as conn:
                observed.append((_receipt(conn, 'github_event', event_id),
                                 conn.execute('SELECT count(*) FROM tasks').fetchone()[0]))
            raise subprocess.CalledProcessError(1, 'git', stderr='preparation failed')

        with patch.object(assigned_workspace, 'prepare_worktree', side_effect=fail_after_commit):
            self.assertFalse(self.cycle(fixture)['ok'])
        (before_preparation, task_count), = observed
        self.assertTrue(before_preparation['received_at'])
        self.assertIsNone(before_preparation.get('disposition'))
        self.assertEqual(task_count, 0)
        self.assertTrue(self.cycle(fixture)['ok'])
        self.assertTrue(self.cycle(fixture)['ok'])
        with closing(sqlite3.connect(db)) as conn:
            task_id, = conn.execute('SELECT task_id FROM tasks').fetchone()
            self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)
            received = _receipt(conn, 'github_event', event_id)
            self.assertEqual(received['disposition'], 'admitted_task')
            self.assertEqual(received['target_id'], task_id)
            self.assertEqual(received['lineage_key'], _task_lineage(conn, task_id))

    def test_new_github_followup_does_not_inherit_failed_parents_lineage(self):
        fixture = self.fixture(test_run_once.RepositoryCycleFixture)
        self.assertTrue(self.cycle(fixture)['ok'])
        db = fixture.data_dir / 'dd.sqlite3'
        with closing(sqlite3.connect(db)) as conn, conn:
            parent, = conn.execute('SELECT task_id FROM tasks').fetchone()
            original = _task_lineage(conn, parent)
            self.assertTrue(original)
            conn.execute("UPDATE tasks SET lifecycle='failed'")
            conn.execute("UPDATE attempts SET status='failed'")
            conn.execute("UPDATE workstreams SET lifecycle='failed',active_task_id=NULL")
            execution_ownership.close_terminal_task(conn, parent, values.utc_now())
        events = json.loads(fixture.fixture_path.read_text())['events']
        fixture.fixture_path.write_text(json.dumps({'events': [{**events[0], 'id': 'next',
            'body': '@robert-bot please investigate the next issue', 'intent': 'analysis'}]}))
        self.assertTrue(self.cycle(fixture)['ok'])
        with closing(sqlite3.connect(db)) as conn:
            child, = conn.execute('SELECT task_id FROM tasks WHERE parent_task_id=?', (parent,)).fetchone()
            current = _task_lineage(conn, child)
            self.assertTrue(current)
            self.assertNotEqual(current, original)
            self.assertEqual(_receipt(conn, 'github_event', 'event:comment:next')['target_id'], child)
            self.assertEqual(_receipt(conn, 'github_event', 'event:comment:comment-1')['target_id'], parent)

    def test_operator_reply_receipt_and_existing_task_keep_parent_lineage(self):
        fixture = self.fixture(github_board_recovery.GitHubBoardRecoveryFixture)
        with closing(sqlite3.connect(fixture.db)) as conn:
            parent = _task_lineage(conn, fixture.parent)
            self.assertTrue(parent)
            self.assertEqual(_task_lineage(conn, fixture.task_id), parent)
            reply_id, = conn.execute("SELECT event_id FROM work_item_events WHERE event_type='user_response'").fetchone()
            receipt = _receipt(conn, 'work_item_event', reply_id)
            self.assertEqual(receipt['disposition'], 'admitted_task')
            self.assertEqual(receipt['target_id'], fixture.task_id)
            self.assertEqual(receipt['lineage_key'], parent)
        fixture.read()
        fixture.cycle()
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(_task_lineage(conn, fixture.task_id), parent)
            self.assertTrue(conn.execute('SELECT 1 FROM attempts WHERE task_id=?', (fixture.task_id,)).fetchone())

    def test_native_round_has_receipt_before_task_and_transfers_lineage(self):
        fixture = self.fixture(review_coordinator.ReviewCoordinatorFixture)
        with patch.object(ready_work, 'prepare_queued_reviews'):
            fixture.cycle([fixture.event()])
        with closing(sqlite3.connect(fixture.db)) as conn:
            round_id, task_id = conn.execute('SELECT round_id,task_id FROM review_sessions').fetchone()
            self.assertIsNone(task_id)
            receipt = _receipt(conn, 'github_event', 'event:comment:first')
            self.assertEqual(receipt['disposition'], 'queued_native_round')
            self.assertEqual(receipt['target_id'], round_id)
        fixture.cycle([])
        with closing(sqlite3.connect(fixture.db)) as conn:
            task_id, = conn.execute('SELECT task_id FROM review_sessions WHERE round_id=?', (round_id,)).fetchone()
            self.assertTrue(task_id)
            self.assertEqual(_task_lineage(conn, task_id), receipt['lineage_key'])

    def test_superseded_round_replacement_keeps_lineage_across_new_round_id(self):
        fixture = self.fixture(review_coordinator.ReviewCoordinatorFixture)
        fixture.cycle([fixture.event()])
        parent, attempt_id = fixture.current()
        with review_store.connect(fixture.db) as conn:
            round_entry = review_store.session(conn, task_id=parent)
            lineage = _task_lineage(conn, parent)
            now = values.utc_now()
            review_store.update(conn, round_entry['round_id'], status='superseded')
            work_application_lifecycle.finalize_failed_task(conn, parent, round_entry['workstream_id'], now, {'status': 'failed_worker_process_exited'})
            # The dry-run launcher recorded synthetic dispatch metadata; it started no worker.
            conn.execute("""UPDATE attempts SET status='failed',finished_at=?,metadata_json=json_remove(metadata_json,'$.dispatch')
                WHERE attempt_id=?""", (now, attempt_id))
            repo_id, = conn.execute('SELECT repo_id FROM workstreams').fetchone()
        context = work_items.CommandContext('operator', 'maintainer', frozenset({repo_id}), frozenset())
        work_items.retrigger_failed_github_task(fixture.db, parent, context=context, idempotency_key='retry')
        fixture.cycle([])
        child, _ = fixture.current()
        self.assertNotEqual(child, parent)
        with review_store.connect(fixture.db) as conn:
            replacement = review_store.session(conn, task_id=child)
            self.assertNotEqual(replacement['round_id'], round_entry['round_id'])
            self.assertTrue(lineage)
            self.assertEqual(_task_lineage(conn, child), lineage)
            self.assertEqual(replacement['request']['event']['supervision_lineage_key'], lineage)

    def test_old_input_disposition_cannot_overwrite_the_current_generation(self):
        fixture = self.fixture(test_run_once.RepositoryCycleFixture)
        self.assertTrue(self.cycle(fixture)['ok'])
        with closing(sqlite3.connect(fixture.data_dir / 'dd.sqlite3')) as conn, conn:
            event_id, repo_id, source_id, payload = conn.execute(
                'SELECT event_id,repo_id,source_id,payload_json FROM github_events').fetchone()
            event = json.loads(payload)
            now = values.utc_now()
            older = supervision_lineage.github_admission(event, event_id)
            current = supervision_lineage.record_github_received(conn, {**event, 'body': 'Edited instruction'},
                event_id, repo_id, source_id, now)
            self.assertNotEqual(older.generation, current.generation)
            self.assertFalse(supervision_lineage.record_disposition(conn, older, disposition='admitted_task',
                target_type='task', target_id='stale', now=now))
            receipt = _receipt(conn, 'github_event', event_id)
            self.assertEqual(receipt['input_generation'], current.generation)
            self.assertIsNone(receipt.get('disposition'))


if __name__ == '__main__':
    unittest.main()

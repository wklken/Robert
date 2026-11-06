from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import storage, supervision_observe, supervision_rules


NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
OLD = '2026-11-06T09:00:00+00:00'
LATER = '2026-11-06T11:00:00+00:00'


def task_facts(**changes):
    return {'task_id': 'task', 'lifecycle': 'running', 'runnable': True,
            'attempt_id': 'attempt', 'attempt_status': 'running',
            'process_status': 'alive_same', 'started_at': OLD, 'heartbeat_at': OLD, **changes}


def evaluate(rule, facts):
    return next(row for row in supervision_rules.evaluate_task(facts, now=NOW) if row['rule_id'] == rule)


class SupervisionProgressRulesTests(unittest.TestCase):
    def test_expired_daemon_lease_reports_missing_progress_after_grace(self):
        facts = {'scope_id': 'daemon', 'status': 'expired', 'lease_id': 'lease',
                 'lease_expires_at': OLD}
        row = supervision_rules.evaluate_controller(facts, now=NOW)
        self.assertEqual((row['classification'], row['reason_code']),
                         ('confirmed', 'controller_progress_stalled'))
        self.assertNotIn('positive_evidence', row)

    def test_stale_work_starts_eligibility_clock_when_backoff_ends(self):
        facts = task_facts(pending_action_id='action', publication_eligible=True,
                           action_created_at=OLD, publication_retry_at=LATER)
        waiting = evaluate('publication_stalled', facts)
        self.assertIsNone(waiting['eligible_since'])
        facts.update(publication_retry_at=OLD, checkpoints={'publication_stalled': waiting})
        ready = evaluate('publication_stalled', facts)
        self.assertEqual(ready['classification'], 'expected_wait')
        self.assertEqual(ready['eligible_since'], NOW.isoformat())

    def test_repeated_failed_activity_does_not_reset_worker_progress(self):
        facts = task_facts(milestones=[['review_result', 'known-digest']],
                           milestone_observed_at=OLD, command_failures=2)
        first = evaluate('worker_no_progress', facts)
        facts.update(command_failures=9, heartbeat_at=NOW.isoformat())
        second = evaluate('worker_no_progress', facts)
        self.assertEqual(second['progress_token'], first['progress_token'])
        facts['milestones'].append(['review_report', 'new-digest'])
        self.assertNotEqual(evaluate('worker_no_progress', facts)['progress_token'], first['progress_token'])

    def test_prepared_capacity_wait_does_not_become_ready_stall(self):
        row = evaluate('ready_not_dispatched', task_facts(lifecycle='queued', attempt_status='prepared',
                       dispatch_block='repository_capacity', has_prompt=True, has_worktree=True))
        self.assertEqual(row['classification'], 'expected_wait')
        self.assertEqual(row['reason_code'], 'repository_capacity')
        self.assertIsNone(row['eligible_since'])

    def test_complete_delivery_with_a_missing_planned_action_is_confirmed(self):
        result = evaluate('delivery_inconsistent', task_facts(lifecycle='completed', attempt_status='completed',
                          result_id='result', delivery_status='inconsistent',
                          delivery_reason='planned_actions_do_not_match', planned_action_count=2, action_count=1))
        self.assertEqual(result['classification'], 'confirmed')
        self.assertNotIn('positive_evidence', result)


class SupervisionAdmissionObservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'state.db'
        storage.init_database(self.db)
        self.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','default','main','/tmp','/tmp')")
        self.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number) VALUES('source','repo','example/repo#1','issue',1)")

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(sql, params)

    def receipt(self, key, **fields):
        metadata = {'received_at': OLD, 'input_generation': 'generation', 'source_id': 'source',
                    'repo_id': 'repo', 'lineage_key': 'lineage', **fields}
        self.execute("INSERT INTO supervision_checkpoints(scope_type,scope_id,rule_id,lineage_key,updated_at,metadata_json) VALUES('github_event',?,'admission','lineage',?,?)",
                     (key, OLD, json.dumps(metadata)))

    def test_missing_and_deferred_dispositions_are_distinct_and_never_claim_progress(self):
        self.receipt('missing')
        self.receipt('deferred', disposition='deferred', consumer='github_route', due_at=LATER)
        rows = supervision_observe.collect(self.db, now=NOW)['observations']
        by_scope = {row['scope_id']: row for row in rows}
        self.assertEqual(by_scope['missing']['rule_id'], 'accepted_input_unowned')
        self.assertEqual(by_scope['missing']['evidence']['input_generation'], 'generation')
        self.assertNotIn('positive_evidence', by_scope['missing'])
        self.assertEqual(by_scope['deferred']['classification'], 'expected_wait')
        self.assertEqual(by_scope['deferred']['next_check_at'], LATER)

    def test_admission_and_task_pages_both_advance_at_batch_size_one(self):
        for key in ('a', 'b', 'c'):
            self.receipt('receipt-' + key)
            self.execute("INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES(?,'repo',?,?,?)", ('stream-' + key, key, OLD, OLD))
            self.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,'running',?,?)", (key, 'stream-' + key, OLD, OLD))
        seen = []
        cursor = ''
        for _ in range(6):
            page = supervision_observe.collect(self.db, now=NOW, limit=1, cursor=cursor)
            scopes = {row['scope_id'] for row in page['observations'] if row['scope_type'] != 'controller'}
            self.assertEqual(len(scopes), 1)
            seen.extend(scopes)
            cursor = page['cursor']
        self.assertTrue(page['coverage_complete'])
        self.assertTrue(any(scope.startswith('receipt-') for scope in seen[:2]))
        self.assertEqual(set(seen), {'a', 'b', 'c', 'receipt-a', 'receipt-b', 'receipt-c'})

    def test_admission_receipt_change_invalidates_decisive_snapshot(self):
        self.receipt('missing')
        row = next(item for item in supervision_observe.collect(self.db, now=NOW)['observations']
                   if item['scope_id'] == 'missing')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(supervision_observe.is_current(conn, row))
        self.execute("""UPDATE supervision_checkpoints SET metadata_json=json_set(metadata_json,'$.disposition','explicitly_ignored')
            WHERE scope_type='github_event' AND scope_id='missing' AND rule_id='admission'""")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(supervision_observe.is_current(conn, row))

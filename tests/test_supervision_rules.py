from datetime import datetime, timezone
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import supervision_rules


NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
OLD = '2026-11-06T09:00:00+00:00'
LATER = '2026-11-06T11:00:00+00:00'


class SupervisionRulesTests(unittest.TestCase):
    def observations(self, **changes):
        facts = {
            'task_id': 'task', 'lifecycle': 'running', 'runnable': True,
            'attempt_id': 'attempt', 'attempt_status': 'running', 'started_at': OLD,
            'heartbeat_at': OLD, 'process_status': 'unknown',
            **changes,
        }
        return {row['rule_id']: row for row in supervision_rules.evaluate_task(facts, now=NOW)}

    def test_unknown_worker_is_not_declared_dead(self):
        row = self.observations()['worker_state_mismatch']
        self.assertEqual(row['classification'], 'unobservable')
        self.assertNotIn('positive_evidence', row)

    def test_active_worker_group_is_not_replaced_after_leader_exit(self):
        row = self.observations(process_status='group_alive')['worker_state_mismatch']
        self.assertEqual(row['classification'], 'expected_wait')
        self.assertNotIn('positive_evidence', row)

    def test_old_dead_worker_without_result_is_confirmed(self):
        row = self.observations(process_status='dead', spool_status='missing')['worker_state_mismatch']
        self.assertEqual(row['classification'], 'confirmed')
        self.assertEqual(row['evidence']['attempt_id'], 'attempt')

    def test_accepted_result_is_positive_only_after_process_cleanup(self):
        facts = {'result_id': 'result', 'audit_status': 'accepted', 'result_created_at': OLD, 'audited_at': OLD}
        row = self.observations(**facts)['result_not_consumed']
        self.assertEqual(row['classification'], 'expected_wait')
        self.assertNotIn('positive_evidence', row)
        row = self.observations(**facts, attempt_status='completed', process_stopped_at=OLD)['result_not_consumed']
        self.assertEqual(row['positive_evidence']['result_id'], 'result')

    def test_backoff_suppresses_publication_and_handoff_age(self):
        rows = self.observations(
            pending_action_id='action', publication_eligible=False, publication_retry_at=LATER,
            handoff_status='pending', handoff_retry_at=LATER,
        )
        for rule in ('publication_stalled', 'handoff_stalled'):
            self.assertEqual(rows[rule]['classification'], 'expected_wait')
            self.assertNotIn('positive_evidence', rows[rule])

    def test_unknown_external_effect_does_not_claim_delivery(self):
        row = self.observations(pending_action_id='action', intent_status='executing',
                                publication_eligible=True, action_created_at=OLD)['publication_stalled']
        self.assertEqual(row['classification'], 'expected_wait')
        self.assertNotIn('positive_evidence', row)

    def test_completed_handoff_requires_recorded_successor(self):
        row = self.observations(handoff_status='completed')['handoff_stalled']
        self.assertNotIn('positive_evidence', row)
        row = self.observations(handoff_status='completed', successor_task_id='child', successor_occurred_at=OLD)['handoff_stalled']
        self.assertEqual(row['positive_evidence']['successor_task_id'], 'child')

    def test_heartbeat_changes_do_not_change_progress_token(self):
        first = self.observations(process_status='alive_same')['worker_state_mismatch']
        second = self.observations(process_status='alive_same', heartbeat_at=LATER)['worker_state_mismatch']
        self.assertEqual(first['progress_token'], second['progress_token'])

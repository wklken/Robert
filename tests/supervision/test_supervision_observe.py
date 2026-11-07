from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision.application import observation as supervision_observe


NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
OLD = '2026-11-06T09:00:00+00:00'
LATER = '2026-11-06T11:00:00+00:00'


class SupervisionObservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / 'state.db'
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','default','main','/tmp','/tmp')")

    def task(self, task_id, lifecycle='running'):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES(?,'repo',?,?,?)", ('stream-' + task_id, task_id, OLD, OLD))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,?,?,?)", (task_id, 'stream-' + task_id, lifecycle, OLD, OLD))

    def execute(self, statement, args=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(statement, args)

    def observations(self):
        return supervision_observe.collect(self.db, now=NOW)['observations']

    def test_missing_database_is_not_created(self):
        absent = self.db.with_name('absent.db')
        with self.assertRaises(supervision_observe.ObservationUnavailable) as caught:
            supervision_observe.collect(absent, now=NOW)
        self.assertEqual(caught.exception.reason_code, 'database_unavailable')
        self.assertFalse(absent.exists())

    def test_corrupt_database_has_an_explicit_error(self):
        broken = self.db.with_name('broken.db')
        broken.write_bytes(b'invalid SQLite database')
        with self.assertRaises(supervision_observe.ObservationUnavailable) as caught:
            supervision_observe.collect(broken, now=NOW)
        self.assertEqual(caught.exception.reason_code, 'database_corrupt')

    def test_database_error_reasons_without_sqlite_errorname(self):
        cases = {
            'file is not a database': 'database_corrupt',
            'database disk image is malformed': 'database_corrupt',
            'database is locked': 'database_busy',
            'database table is locked: leases': 'database_busy',
            'interrupted': 'database_observation_deadline',
            'no such table: leases': 'database_schema_incompatible',
            'no such column: private_column': 'database_schema_incompatible',
            'disk I/O error': 'database_unavailable',
        }
        for message, reason in cases.items():
            with self.subTest(message=message), patch.object(sqlite3, 'connect', side_effect=sqlite3.DatabaseError(message)):
                with self.assertRaises(supervision_observe.ObservationUnavailable) as caught:
                    supervision_observe.collect(self.db, now=NOW)
                self.assertEqual(caught.exception.reason_code, reason)
                self.assertEqual(str(caught.exception), reason)

    def test_task_pages_cover_stable_ids_without_repeating_tasks(self):
        for name in ('c', 'a', 'b'):
            self.task(name)
        seen = []
        cursor = ''
        for _ in range(3):
            page = supervision_observe.collect(self.db, now=NOW, limit=1, cursor=cursor)
            scopes = {row['scope_id'] for row in page['observations'] if row['scope_type'] == 'task'}
            self.assertEqual(len(scopes), 1)
            seen.extend(scopes)
            cursor = page['cursor']
            if page['coverage_complete']:
                break
        self.assertEqual(seen, ['a', 'b', 'c'])
        self.assertTrue(page['coverage_complete'])
        self.assertEqual(cursor, '')

    def test_publication_waits_for_recorded_exit_and_retry_due(self):
        self.task('a')
        self.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES('attempt','a',1,'completed','{\"dispatch\":{\"pid\":123}}')")
        self.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES('result','a','attempt','comment_analysis',?,'{\"audit\":{\"status\":\"accepted\"}}')", (OLD,))
        self.execute("INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,created_at,metadata_json) VALUES('action','result','a','comment','accepted',?,json_object('publish',json_object('next_retry_at',?)))", (OLD, LATER))
        with patch.object(supervision_observe, '_observe_process', return_value='dead'):
            rows = self.observations()
        publication = next(row for row in rows if row['rule_id'] == 'publication_stalled')
        self.assertEqual(publication['classification'], 'expected_wait')
        self.assertNotIn('positive_evidence', publication)

    def test_bound_publication_receipt_is_positive_after_confirmed_action(self):
        self.task('a')
        self.execute("UPDATE tasks SET lifecycle='completed',expected_output='comment_analysis' WHERE task_id='a'")
        self.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','a',1,'completed')")
        url = 'https://github.com/example/repo/issues/1'
        self.execute("""INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json)
            VALUES('result','a','attempt','comment_analysis',?,json_object(
                'audit',json_object('status','accepted','action_scope',json_object('repo','example/repo')),
                'normalized_payload',json_object('planned_github_actions',json_array(json_object('type','comment')))))""", (OLD,))
        receipt = ('{"schema_version":1,"action_id":"action","result_id":"result","task_id":"a",'
                   '"action_type":"comment","scope":{"repo":"example/repo"},"external_id":"external",'
                   f'"target_url":"{url}","confirmed_at":"{OLD}","confirmation_method":"publisher_acknowledgement"}}')
        self.execute("""INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at,external_id,target_url,metadata_json)
            VALUES('action','result','a','comment','accepted','published',?,'external',?,json_object('type','comment',
            'publish',json_object('status','published','published_at',?,'receipt',json(?))))""", (OLD, url, OLD, receipt))
        self.execute("""INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at,remote_evidence_json)
            VALUES('intent','action','key','fingerprint','published',?,?,json_object('status','confirmed_effect','receipt',json(?)))""", (OLD, OLD, receipt))
        publication = next(row for row in self.observations() if row['rule_id'] == 'publication_stalled')
        self.assertEqual(publication['positive_evidence']['result_id'], 'result')
        self.execute("DELETE FROM github_actions WHERE action_id='action'")
        inconsistent = next(row for row in self.observations() if row['rule_id'] == 'delivery_inconsistent')
        self.assertEqual(inconsistent['classification'], 'confirmed')
        self.assertEqual(inconsistent['reason_code'], 'planned_actions_do_not_match')

    def test_latched_lane_stays_failed_until_success_in_a_replacement_daemon(self):
        self.execute("INSERT INTO daemon_runs(daemon_run_id,owner_id,config_path,started_at,status) VALUES('run','run','/tmp/config',?,'running')", (OLD,))
        self.execute("INSERT INTO leases(lease_id,resource_type,resource_key,owner_id,status,acquired_at,expires_at,heartbeat_at) VALUES('lease','daemon',?,'run','active',?,?,?)", ('database:' + str(self.db.resolve()), OLD, LATER, NOW.isoformat()))
        self.execute("INSERT INTO daemon_events(daemon_event_id,daemon_run_id,event_type,status,created_at,metadata_json) VALUES('stop','run','control_plane_attention','attention',?,'{\"control_lane\":\"local\"}')", (OLD,))
        self.execute("INSERT INTO daemon_events(daemon_event_id,daemon_run_id,event_type,status,created_at) VALUES('ok','run','local_drain_completed','ok',?)", (NOW.isoformat(),))
        row = next(row for row in self.observations() if row['scope_id'] == 'daemon:local')
        self.assertEqual(row['classification'], 'confirmed')
        self.assertNotIn('positive_evidence', row)

    def test_spool_ingested_during_probe_does_not_report_missing_result(self):
        self.task('a')
        self.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) VALUES('attempt','a',1,'running',?,?,'{\"dispatch\":{\"pid\":123}}')", (OLD, OLD))

        def ingest_before_probe(*args):
            self.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES('result','a','attempt','comment_analysis',?)", (NOW.isoformat(),))
            return {'spool_status': 'missing'}

        with patch.object(supervision_observe, '_observe_process', return_value='dead'), \
                patch.object(supervision_observe, '_spool_facts', side_effect=ingest_before_probe):
            rows = self.observations()
        worker = next(row for row in rows if row['rule_id'] == 'worker_state_mismatch')
        self.assertEqual(worker['classification'], 'unobservable')

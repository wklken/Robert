from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import execution_store, loop_engine, run_once, storage, worker_identity


class SchedulerRemediationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / '.git').mkdir()
        self.config = self.root / 'config.yml'
        self.data = self.root / 'data'
        self.config.write_text(f'''data_dir: {self.data}
database: runtime.sqlite3
max_concurrency: 3
hard_timeout_minutes: 90
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors: [maintainer]
    default_base_branch: main
    repo_root: {self.repo}
    worktree_root: {self.repo}/.worktrees
''')
        self.db = self.data / 'runtime.sqlite3'
        storage.init_database(self.db)
        self.now = datetime.now(timezone.utc)
        self.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(self.stop_process)
        time.sleep(.1)
        self.identity = worker_identity.capture_process_identity(self.process.pid)
        with self.connect() as conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo:example/repo','example/repo','robot','main',?,?)", (str(self.repo), str(self.repo / '.worktrees')))
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES ('w','repo:example/repo','active','t',?,?)", (self.now.isoformat(), self.now.isoformat()))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,priority,created_at,updated_at) VALUES ('t','w','running','P1',?,?)", (self.now.isoformat(), self.now.isoformat()))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) VALUES ('a','t',1,'running',?,?,?)", (self.now.isoformat(), self.now.isoformat(), json.dumps({'dispatch': self.identity})))
            conn.execute("INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,created_at) VALUES ('phase','a','analyze','running','Working',?)", (self.now.isoformat(),))
            execution_store.ensure_subject(conn, 'task', 't', 'human_request', self.now.isoformat())

    def connect(self):
        connection = sqlite3.connect(self.db)
        self.addCleanup(connection.close)
        return connection

    def stop_process(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def runnable(self, **kwargs):
        with self.connect() as conn:
            return loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/repo'], **kwargs)

    def result(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES ('result','t','a','comment_analysis',?)", (self.now.isoformat(),))
            conn.execute("INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,task_id,attempt_id,result_id,status,not_before_at,created_at,updated_at) VALUES ('wake','repo:example/repo','worker_result_ready','result','t','a','result','pending',?,?,?)", (self.now.isoformat(),) * 3)

    def test_healthy_worker_does_not_create_business_runs(self):
        outcome = loop_engine.run_loop(self.config, dry_run=False, skip_external=True, skip_publish=True)
        self.assertEqual(outcome['cycles'], 0)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 0)

    def test_dead_worker_is_runnable_without_waiting_for_old_heartbeat(self):
        self.stop_process()
        self.assertTrue(self.runnable())

    def test_observation_is_read_only_and_does_not_expire_wakeups(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,status,not_before_at,expires_at,created_at,updated_at) VALUES ('old','repo:example/repo','manual_operator_request','old','pending','2020-01-01','2020-01-02','2020-01-01','2020-01-01')")
        with self.connect() as conn:
            conn.execute('PRAGMA query_only=ON')
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/repo']))
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT status FROM wakeups WHERE wakeup_id='old'").fetchone()[0], 'pending')

    def test_already_stale_alive_worker_does_not_trigger_duplicate_transition(self):
        old = (self.now - timedelta(minutes=25)).isoformat()
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET started_at=?,heartbeat_at=?", (old, old))
        self.assertTrue(self.runnable())
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET status='stale'")
        self.assertFalse(self.runnable())
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET heartbeat_at=?", (datetime.now(timezone.utc).isoformat(),))
        self.assertTrue(self.runnable())

    def test_pending_result_waits_for_process_exit_without_repeated_drain(self):
        self.result()
        self.assertFalse(self.runnable())
        self.stop_process()
        self.assertTrue(self.runnable())

    def test_completed_legacy_result_still_waits_for_recorded_process(self):
        self.result()
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET status='completed'")
        self.assertFalse(self.runnable())
        self.stop_process()
        self.assertTrue(self.runnable())

    def test_prepared_backoff_waits_until_due(self):
        future = (self.now + timedelta(minutes=5)).isoformat()
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET status='prepared',metadata_json=?", (json.dumps({'resume': {'not_before_at': future}}),))
        self.assertFalse(self.runnable())
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET metadata_json='{}'")
        self.assertTrue(self.runnable())

    def test_queued_vote_is_discovered_but_cross_workflow_attention_blocks_it(self):
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
            conn.execute("INSERT INTO vote_sessions(round_id,repo,number,source_type,workstream_id,trigger_fingerprint,request_json,created_at,updated_at) VALUES ('v','example/repo',1,'issue','w','comment:1','{}',?,?)", (self.now.isoformat(),) * 2)
        self.assertTrue(self.runnable())
        with self.connect() as conn:
            conn.execute("INSERT INTO review_sessions(round_id,repo,pr_number,workstream_id,trigger_fingerprint,status,request_json,created_at,updated_at) VALUES ('r','example/repo',1,'w','comment:2','attention','{}',?,?)", (self.now.isoformat(),) * 2)
        self.assertFalse(self.runnable())

    def test_attempt_timeout_uses_real_loop_configuration(self):
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET started_at=?", ((self.now - timedelta(minutes=50)).isoformat(),))
        self.assertTrue(self.runnable())  # The default global limit is 45 minutes.
        outcome = loop_engine.run_loop(self.config, dry_run=False, skip_external=True, skip_publish=True)
        self.assertEqual(outcome['cycles'], 0)  # This config deliberately permits 90 minutes.

    def test_orphaned_classification_is_discovered_and_respects_backoff(self):
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
            conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES ('classification','t','a','classification_result',?,?)", (self.now.isoformat(), json.dumps({'recommended_route': 'new-pr', 'audit': {'status': 'accepted'}})))
        self.assertTrue(self.runnable())
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET metadata_json=?", (json.dumps({'classification_handoff': {'retryable': True, 'next_retry_at': (self.now + timedelta(minutes=5)).isoformat()}}),))
        self.assertFalse(self.runnable())

    def test_attention_binding_is_not_restarted_by_process_death(self):
        self.stop_process()
        with self.connect() as conn:
            conn.execute("UPDATE execution_subjects SET binding_status='attention'")
        self.assertFalse(self.runnable())

    def test_unknown_identity_requests_fenced_supervision_immediately(self):
        with patch.object(run_once, '_dispatch_process_status', return_value={'status': 'unknown'}):
            with self.connect() as conn:
                conn.execute('PRAGMA query_only=ON')
                self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/repo']))
                self.assertFalse(conn.in_transaction)

    def test_startup_evidence_is_checked_without_backfilling_during_probe(self):
        with self.connect() as conn:
            conn.execute('DELETE FROM worker_phases')
            conn.execute('UPDATE attempts SET started_at=?', ((self.now - timedelta(minutes=6)).isoformat(),))
        self.assertTrue(self.runnable())
        directory = self.data / 'artifacts' / 't'
        directory.mkdir(parents=True)
        (directory / 'a.analyze.command.json').write_text(json.dumps({
            'attempt_id': 'a', 'task_id': 't', 'phase': 'analyze', 'status': 'completed',
            'finished_at': self.now.isoformat(), 'snapshot_status': 'deferred',
            'summary': 'Command exited 0',
        }))
        with self.connect() as conn:
            conn.execute('PRAGMA query_only=ON')
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/repo']))
            self.assertFalse(conn.in_transaction)
            self.assertEqual(conn.execute('SELECT count(*) FROM worker_phases').fetchone()[0], 0)

    def test_result_for_canceled_task_does_not_trigger_publication_drain(self):
        self.result()
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled'")
        self.assertFalse(self.runnable())

    def test_native_queue_scope_does_not_leak_other_repository(self):
        with self.connect() as conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('other','example/other','robot','main','/other','/other/worktrees')")
            conn.execute("INSERT INTO vote_sessions(round_id,repo,number,source_type,workstream_id,trigger_fingerprint,request_json,created_at,updated_at) VALUES ('foreign','example/other',1,'issue','foreign','comment:9','{}',?,?)", (self.now.isoformat(),) * 2)
        with self.connect() as conn:
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=['repo:example/repo']))
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=['other']))

    def test_scope_config_return_remains_backwards_compatible(self):
        self.assertEqual(len(loop_engine.load_config_scope(self.config, skip_external=True)), 2)
        db, repos, config = loop_engine.load_config_scope(self.config, skip_external=True, include_config=True)
        self.assertEqual(db, self.db)
        self.assertEqual(repos, ['repo:example/repo'])
        self.assertEqual(config['hard_timeout_minutes'], 90)

    def test_launch_specific_timeout_and_supplied_observation_time_are_respected(self):
        with self.connect() as conn:
            identity = {**self.identity, 'worker_launch': {'timeout_seconds': 30}}
            conn.execute('UPDATE attempts SET metadata_json=?', (json.dumps({'dispatch': identity}),))
        self.assertTrue(self.runnable(config_result={'hard_timeout_minutes': 90},
                                      now=self.now + timedelta(seconds=31)))

    def test_completed_result_without_owned_process_can_advance(self):
        self.result()
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET status='completed',metadata_json='{}'")
        self.assertTrue(self.runnable())

    def test_rejected_prepared_result_requests_supervision_without_start_times(self):
        with self.connect() as conn:
            conn.execute("UPDATE attempts SET status='prepared',started_at=NULL,heartbeat_at=NULL,metadata_json=?", (json.dumps({'result_validation': {'status': 'failed_validation', 'terminate': True}}),))
        self.assertTrue(self.runnable())

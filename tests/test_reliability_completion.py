"""Acceptance gaps left by the first reliability delivery."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import retry_policy, run_once


class RetryClassificationTests(unittest.TestCase):
    def test_terminal_rate_limit_ignores_quoted_recovered_and_superseded_errors(self):
        import json
        failed = json.dumps({'type': 'turn.failed', 'error': {'message': '429 Too Many Requests'}})
        quoted = json.dumps({'type': 'item.completed', 'item': {'aggregated_output': failed}})
        for tail in (quoted, failed + '\n' + json.dumps({'type': 'turn.completed'}),
                     failed + '\n' + json.dumps({'type': 'turn.failed', 'error': {'message': '401 Unauthorized'}}),
                     failed + '\n' + json.dumps({'type': 'result', 'is_error': False})):
            with self.subTest(tail=tail):
                self.assertIsNone(retry_policy.terminal_rate_limit({'stdout': {'tail': tail}}))
        for tail in (failed, json.dumps({'type': 'result', 'is_error': True, 'errors': ['429 Too Many Requests']})):
            self.assertIsNotNone(retry_policy.terminal_rate_limit({'stdout': {'tail': tail}}))
        self.assertIsNotNone(retry_policy.terminal_rate_limit({'stderr': {'tail': 'HTTP 429 Too Many Requests'}}))

    def test_permission_403_and_rate_limit_403_are_distinct(self):
        self.assertEqual(retry_policy.error_class('HTTP 403 Forbidden'), 'permission_required')
        self.assertEqual(retry_policy.error_class('HTTP 403: API rate limit exceeded'), 'rate_limited')
        self.assertEqual(retry_policy.error_class('HTTP 429 Too Many Requests'), 'rate_limited')

    def test_retry_after_is_respected_without_extending_budget(self):
        now = '2026-10-24T00:00:00+00:00'
        state = retry_policy.failure_state({}, 'HTTP 429\nRetry-After: 120', now)
        self.assertTrue(state['retryable'])
        self.assertEqual(state['next_retry_at'], '2026-10-24T00:02:00+00:00')
        state = retry_policy.failure_state({}, 'HTTP 429\nRetry-After: Sat, 24 Oct 2026 00:03:00 GMT', now)
        self.assertEqual(state['next_retry_at'], '2026-10-24T00:03:00+00:00')
        state = retry_policy.failure_state({}, 'HTTP 429\nRetry-After: 900', now)
        self.assertFalse(state['retryable'])
        self.assertEqual(state['reason_code'], 'retry_budget_exhausted')

    def test_malformed_retry_after_does_not_crash_or_disable_budget(self):
        state = retry_policy.failure_state({}, 'HTTP 429\nRetry-After: invalid', '2026-10-24T00:00:00+00:00')
        self.assertEqual(state['next_retry_at'], '2026-10-24T00:00:05+00:00')

    def test_worker_error_uses_terminal_protocol_not_quoted_content(self):
        import json
        failed = json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
        quoted = json.dumps({"type": "item.completed", "item": {"text": "401 Unauthorized"}})
        self.assertEqual(retry_policy.error_class(retry_policy.worker_error({"worker_stdout": {"tail": failed}})), "authentication_required")
        self.assertEqual(retry_policy.worker_error({"worker_stdout": {"tail": quoted}}), "")
        self.assertEqual(retry_policy.worker_error({"worker_stdout": {"tail": failed + '\n' + json.dumps({"type": "turn.completed"})}}), "")

    def test_error_only_logs_are_not_progress(self):
        with patch.object(run_once, '_attempt_command_records', return_value=[]), \
             patch.object(run_once, '_latest_worker_phases', return_value=[]), \
             patch.object(run_once, '_attempt_log_tails', return_value={'worker_stderr': {'tail':'HTTP 503 service unavailable'}}), \
             patch.object(run_once, '_git_recovery_summary', return_value={}):
            evidence = run_once._inspect_attempt_recovery(None, 'a', 't', None)
        self.assertEqual(evidence['status'], 'no_progress')
        self.assertEqual(evidence['progress_signals'], [])


class TimingTests(unittest.TestCase):
    def test_timings_keep_queue_execution_and_ingestion_separate(self):
        from tests.test_reliability import DurableResultTests
        from robert_agent.worker import result
        from robert_agent import execution_metrics
        fixture = DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertTrue(result.record_result(fixture.db, fixture.payload)['ok'])
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET created_at='2026-10-24T00:00:00Z'")
            conn.execute("UPDATE attempts SET started_at='2026-10-24T00:01:00Z', finished_at='2026-10-24T00:04:00Z',metadata_json=json_set(metadata_json,'$.worker_started_at','2026-10-24T00:01:00Z')")
            conn.execute("UPDATE worker_results SET metadata_json=json_set(metadata_json, '$.result_saved_at','2026-10-24T00:03:00Z','$.result_ingested_at','2026-10-24T00:04:00Z')")
            timing = execution_metrics.task_timing(conn, 'task-1')
            self.assertEqual(timing['queue_seconds'], 60)
            self.assertEqual(timing['worker_seconds'], 120)
            self.assertEqual(timing['result_ingestion_seconds'], 60)
            self.assertIsNone(timing['result_to_publication_seconds'])
            report = execution_metrics.summarize(conn)
            group = next(iter(report['by_task_kind'].values()))
            self.assertEqual(group['queue_seconds']['p95'], 60)


class IsolatedDaemonTests(unittest.TestCase):
    def test_daemon_restart_reconciles_real_git_write_and_delivers_notification(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time
        from tests.test_reliability import PublicationBudgetTests
        from robert_agent import execution_store
        fixture = PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(fixture.tmp.name)
        def git(*args):
            return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-b', 'main')
        (root / 'example.txt').write_text('isolated acceptance\n')
        git('add', 'example.txt')
        git('-c', 'user.name=Acceptance', '-c', 'user.email=acceptance@example.invalid', 'commit', '-m', 'Isolated fixture')
        sha = git('rev-parse', 'HEAD')
        git('init', '--bare', str(root / 'remote.git'))
        git('remote', 'add', 'origin', str(root / 'remote.git'))
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            metadata = json.loads(conn.execute('SELECT metadata_json FROM worker_results').fetchone()[0])
            metadata['audit']['action_scope']['result_head_sha'] = sha
            conn.execute('UPDATE worker_results SET metadata_json=?', (json.dumps(metadata),))
            conn.execute("UPDATE repos SET repo_id='repo:x/y' WHERE repo_id='repo'")
            conn.execute("UPDATE workstreams SET repo_id='repo:x/y',active_task_id='t'")
            execution_store.ensure_subject(conn, 'task', 't', 'human_request', fixture.now.isoformat())
            conn.execute("INSERT INTO notifications(notification_id,task_id,notification_type,channel,status,created_at) VALUES ('notice','t','isolated','local','recorded',?)",(fixture.now.isoformat(),))
        (root/'daemon.yml').write_text(f'''data_dir: {root}
database: db
max_concurrency: 1
repos:
  - full_name: x/y
    github_account: isolated
    trusted_actors: [isolated]
    default_base_branch: main
    repo_root: {root}
    worktree_root: {root}/worktrees
daemon:
  enabled: true
  local_poll_seconds: 1
''')
        (root/'receiver.py').write_text('import sys,json\nfrom pathlib import Path\np=Path(__file__).with_name("deliveries.jsonl")\nv=json.load(sys.stdin)\nwith p.open("a") as f:f.write(json.dumps(v)+"\\n")\n')
        import shlex
        env = {**os.environ,
               'GH_CONFIG_DIR': str(root/'empty-gh-config'),
               'GH_TOKEN': '', 'GITHUB_TOKEN': '',
               'ROBERT_NOTIFICATION_COMMAND':shlex.join([sys.executable,str(root/'receiver.py')])}
        child = str(Path(__file__).with_name('reliability_daemon_child.py'))
        with (root/'daemon.log').open('w') as log:
            first = subprocess.Popen([sys.executable, child, str(root), 'crash'], stdout=log, stderr=log, env=env)
            def stop(process):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            self.addCleanup(stop, first)
            self.assertEqual(first.wait(timeout=30), 73, (root/'daemon.log').read_text())
            with closing(sqlite3.connect(fixture.db)) as conn, conn:
                self.assertEqual(conn.execute('SELECT status FROM publication_intents').fetchone()[0], 'executing')
                # Simulate the lease grace period after confirmed process death.
                conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE resource_type IN ('daemon','agent_run')")
            second = subprocess.Popen([sys.executable, child, str(root), 'recover'], stdout=log, stderr=log, env=env)
            self.addCleanup(stop, second)
            deadline = time.monotonic()+30
            complete = False
            while time.monotonic()<deadline and second.poll() is None:
                with closing(sqlite3.connect(fixture.db)) as conn:
                    complete = conn.execute("SELECT status FROM publication_intents").fetchone()[0]=='published'
                    binding = conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0]
                if complete and binding=='terminal':
                    break
                time.sleep(.1)
            self.assertTrue(complete and binding=='terminal', (root/'daemon.log').read_text())
            second.terminate()
            self.assertEqual(second.wait(timeout=15),0,(root/'daemon.log').read_text())
        calls = [json.loads(line) for line in (root/'calls.jsonl').read_text().splitlines()]
        self.assertEqual(sum(command[:2]==['git','push'] for command in calls),1)
        self.assertIn(sha,git('ls-remote','origin','refs/heads/codex/fix'))
        deliveries = [json.loads(line) for line in (root/'deliveries.jsonl').read_text().splitlines()]
        self.assertEqual(len([v for v in deliveries if v['type']=='isolated']),1)
        with closing(sqlite3.connect(fixture.db)) as conn:
            delivery=json.loads(conn.execute("SELECT metadata_json FROM notifications WHERE notification_id='notice'").fetchone()[0])['delivery']
            self.assertEqual(delivery['status'],'sent')


class DeliveryAndRecoveryTests(unittest.TestCase):
    def test_old_stopped_actions_cannot_starve_later_ready_result(self):
        from tests.test_reliability import PublicationBudgetTests
        from robert_agent import publish
        fixture = PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish.retryable',0)")
            for i in range(25):
                conn.execute("INSERT INTO github_actions SELECT ?,result_id,task_id,action_type,target_url,external_id,audit_status,publish_status,created_at,metadata_json FROM github_actions WHERE action_id='push'", ('stopped-'+str(i),))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a2','t',2,'completed')")
            conn.execute("INSERT INTO worker_results SELECT 'r2',task_id,'a2',output_type,consumed_event_fingerprints_json,verification_json,handoff,created_at,metadata_json FROM worker_results WHERE result_id='r'")
            conn.execute("INSERT INTO github_actions SELECT 'healthy','r2',task_id,action_type,target_url,external_id,audit_status,publish_status,'zzz',json_remove(metadata_json,'$.publish') FROM github_actions WHERE action_id='push'")
            ready = publish._ready_actions(conn,20)
            self.assertEqual([row['action_id'] for row in ready],['healthy'])

    def test_commit_then_process_death_replays_without_duplicate_actions(self):
        import json
        import subprocess
        import sys
        from pathlib import Path
        from tests.test_reliability import DurableResultTests
        from robert_agent.worker import result
        fixture=DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        payload=Path(fixture.db).with_suffix('.payload.json')
        payload.write_text(json.dumps(fixture.payload))
        program='''import json,os,sys
from robert_agent import result_spool
from robert_agent.worker import result
p=json.load(open(sys.argv[2]))
def writer(db,payload,notifier=None):
 response=result._record_result_db(db,payload,notifier=notifier)
 assert response['ok'], response
 os._exit(73)
result_spool.submit(sys.argv[1],p,writer,prepare=result._prepare,lookup=result._lookup)
'''
        child=subprocess.run([sys.executable,'-c',program,str(fixture.db),str(payload)],capture_output=True,text=True,timeout=15)
        self.assertEqual(child.returncode,73,child.stderr)
        self.assertEqual(result.replay_pending_results(fixture.db)['recorded'],1)
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM worker_results').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT count(*) FROM wakeups').fetchone()[0],1)

    def test_30_second_write_lock_preserves_offline_result(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        import time
        from tests.test_reliability import DurableResultTests
        from robert_agent.worker import result
        fixture=DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        payload=Path(fixture.db).with_suffix('.payload.json')
        payload.write_text(json.dumps(fixture.payload))
        program='''import json,sys
from robert_agent.worker import result
print(json.dumps(result.record_result(sys.argv[1],json.load(open(sys.argv[2])))))
'''
        with closing(sqlite3.connect(fixture.db)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            start=time.monotonic()
            child=subprocess.run([sys.executable,'-c',program,str(fixture.db),str(payload)],capture_output=True,text=True,timeout=15)
            self.assertEqual(child.returncode,0,child.stderr)
            self.assertEqual(json.loads(child.stdout)['status'],'deferred')
            time.sleep(max(0,30-(time.monotonic()-start)))
            blocker.rollback()
        self.assertEqual(result.replay_pending_results(fixture.db)['recorded'],1)
        self.assertEqual(result.replay_pending_results(fixture.db)['recorded'],0)
        with closing(sqlite3.connect(fixture.db)) as conn:
            timing=json.loads(conn.execute('SELECT metadata_json FROM worker_results').fetchone()[0])
            from robert_agent.execution_metrics import _seconds
            self.assertGreaterEqual(_seconds(timing['result_saved_at'],timing['result_ingested_at']),29)


class WaitingEvidenceTests(unittest.TestCase):
    def test_repository_lease_wait_is_visible_preserved_then_cleared(self):
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import execution_metrics
        fixture=RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first=run_once.run_once(fixture.config_path,fixture_path=fixture.fixture_path,dry_run=True,skip_external=True)
        self.assertTrue(first['ok'],first)
        db=fixture.data_dir/'dd.sqlite3'
        now=run_once._now()
        with closing(sqlite3.connect(db)) as conn,conn:
            task=conn.execute('SELECT task_id FROM tasks').fetchone()[0]
            lease=run_once._acquire_agent_lease(conn,'repo:example/backend','other-run',now,9)
        saved=None
        for _ in range(2):
            run_once.run_once(fixture.config_path,dry_run=True,skip_external=True,skip_publish=True)
            with closing(sqlite3.connect(db)) as conn:
                timing=execution_metrics.task_timing(conn,task)
                self.assertEqual(timing['waiting_reason'],'repository_busy')
                self.assertIsNotNone(timing['lease_expires_at'])
                if saved:
                    self.assertEqual(timing['waiting_since'],saved)
                saved=timing['waiting_since']
        with closing(sqlite3.connect(db)) as conn,conn:
            run_once._release_agent_lease(conn,lease['lease_id'],run_once._now())
        run_once.run_once(fixture.config_path,dry_run=True,skip_external=True,skip_publish=True)
        with closing(sqlite3.connect(db)) as conn:
            self.assertNotEqual(execution_metrics.task_timing(conn,task)['waiting_reason'],'repository_busy')


class DiscoveryLockTests(unittest.TestCase):
    def test_external_discovery_does_not_hold_domain_writer_lock(self):
        from tests.test_run_once import RunOnceDryRunTests
        fixture=RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        calls=[]
        def discover(*args,**kwargs):
            with closing(sqlite3.connect(fixture.data_dir/'dd.sqlite3',timeout=.1)) as other:
                other.execute('BEGIN IMMEDIATE')
                other.rollback()
            calls.append('unlocked')
            return []
        with patch.object(run_once.discover,'collect_account_notifications',return_value={}), patch.object(run_once.discover,'collect_live_events',side_effect=discover):
            response=run_once.run_once(fixture.config_path,dry_run=True,skip_external=False)
        self.assertTrue(response['ok'],response)
        self.assertEqual(calls,['unlocked'])


class ReviewRegressionTests(unittest.TestCase):
    def test_status_expands_home_for_all_database_reads(self):
        import os
        from tests.test_reliability import DurableResultTests
        from robert_agent import status
        fixture=DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch.dict(os.environ,{'HOME':str(fixture.db.parent)}):
            self.assertIn('execution_timings',status.build_status('~/'+fixture.db.name))

    def test_prepared_and_failed_spawn_are_not_worker_execution(self):
        from tests.test_reliability import DurableResultTests
        from robert_agent import execution_metrics
        fixture=DurableResultTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET created_at='2026-10-24T00:00:00Z'")
            conn.execute("UPDATE attempts SET status='prepared',started_at='2026-10-24T00:00:00Z'")
            self.assertIsNone(execution_metrics.task_timing(conn,'task-1')['queue_seconds'])
            run_once._record_dispatch_result(conn,'attempt-1',{'ok':False,'status':'failed_dispatch','safe_error':'cannot spawn'})
            self.assertIsNone(execution_metrics.task_timing(conn,'task-1')['worker_seconds'])

    def test_terminal_tasks_hide_stale_wait_metadata(self):
        from tests.test_reliability import PublicationBudgetTests
        from robert_agent import execution_metrics
        fixture=PublicationBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled', metadata_json=?", ('{"wait_state":{"reason_code":"repository_busy","since":"now","lease_expires_at":"later"}}',))
            timing=execution_metrics.task_timing(conn,'t')
            self.assertEqual(timing['waiting_reason'],'terminal')
            self.assertIsNone(timing['waiting_since'])
            self.assertIsNone(timing['lease_expires_at'])

    def test_rejected_question_can_be_corrected_without_killing_worker(self):
        import json
        import subprocess
        import sys
        from datetime import datetime, timezone
        from tests.test_worker_protocol import WorkerProtocolTests
        from robert_agent import worker_identity
        from robert_agent.worker import result
        fixture = WorkerProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db = fixture._init_attempt_db()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        self.addCleanup(cleanup)
        identity = worker_identity.capture_process_identity(child.pid)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET started_at=?,heartbeat_at=?,metadata_json=json_set(metadata_json,'$.dispatch',json(?))", (now, now, json.dumps({'status': 'running', **identity})))
        payload = result.build_result(task_id='task-1', attempt_id='attempt-1', output_type='waiting_for_user',
            consumed_event_fingerprints=['comment:1'], verification=[], handoff='question',
            operator_question={'kind': 'clarification', 'summary': 'Which version?', 'choices': []})
        rejected = result.record_result(db, payload)
        self.assertEqual(rejected['status'], 'failed_validation')
        self.assertIsNone(child.poll())
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute('SELECT status,failure_json FROM attempts').fetchone(), ('running', None))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM worker_results').fetchone()[0], 0)
        payload['submission']['planned_github_actions'] = [{'type': 'comment', 'body': 'Which version should I target?'}]
        accepted = result.record_result(db, payload)
        self.assertTrue(accepted['ok'], accepted)
        self.assertIsNone(child.poll())
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'running')
        child.terminate()
        child.wait(timeout=10)
        with closing(sqlite3.connect(db)) as conn, conn:
            run_once._supervise_running_attempts(conn, 'repo-1', {}, now)
            self.assertEqual(conn.execute('SELECT status FROM attempts').fetchone()[0], 'completed')

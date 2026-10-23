from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import daemon, daemon_state, run_once, status, storage, web
from robert_agent.validate_config import DAEMON_DEFAULTS


NOW = '2026-10-23T00:00:00+00:00'
OLD = '2026-08-01T00:00:00+00:00'
RECENT_FAILURE = '2026-10-01T00:00:00+00:00'


class RetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)
        with self.connect() as conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','example/repo','bot','main','/repo','/worktrees')")
            daemon_state.start_daemon_run(conn, config_path='/config.yml', owner_id='daemon', now=OLD)
            self.lease = daemon_state.acquire_daemon_lease(conn, resource_key='database', owner_id='daemon', ttl_seconds=60, now=OLD)['lease_id']
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('ws','repo','active',?,?)", (OLD, OLD))
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('task','ws','running',?,?)", (OLD, OLD))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at) VALUES ('attempt','task',1,'running',?)", (OLD,))
            conn.execute("INSERT INTO audit_events(audit_id,task_id,event_type,created_at,payload_json) VALUES ('audit','task','accepted',?,'{}')", (OLD,))
        self.context = daemon.DaemonContext(config_path='/config.yml', workflow_path=None, db_path=self.db, repo_id='repo', max_concurrency=3, daemon_config=dict(DAEMON_DEFAULTS), daemon_run_id='daemon', lease_id=self.lease)

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(conn.close)
        return conn

    def add_run(self, name, state='completed', when=OLD, finished=True):
        with self.connect() as conn:
            conn.execute('INSERT INTO agent_runs(run_id,status,started_at,finished_at) VALUES (?,?,?,?)', (name, state, when, when if finished else None))
            conn.execute("INSERT INTO run_steps(step_id,run_id,step_key,status) VALUES (?,?,'summarize','succeeded')", ('s-'+name, name))
            conn.execute("INSERT INTO run_repo_steps(step_id,run_id,repo_id,step_key,status) VALUES (?,?,'repo','route','succeeded')", ('rs-'+name, name))

    def add_event(self, name, state='ok', when=OLD):
        with self.connect() as conn:
            conn.execute('INSERT INTO daemon_events(daemon_event_id,daemon_run_id,event_type,status,created_at) VALUES (?,\'daemon\',\'poll\',?,?)', (name, state, when))

    def add_lease(self, name, state='released', owner='ended', when=OLD):
        with self.connect() as conn:
            conn.execute("INSERT INTO leases(lease_id,resource_type,resource_key,owner_id,status,acquired_at,expires_at,heartbeat_at,released_at) VALUES (?,'agent_run',?,?,?,?,?,?,?)", (name, name, owner, state, when, when, when, when))

    def core_rows(self):
        with self.connect() as conn:
            return {t: conn.execute('SELECT * FROM '+t).fetchall() for t in ('tasks','attempts','audit_events','wakeups','workstreams')}

    def test_heartbeat_renews_lease_without_growing_event_history(self):
        daemon._heartbeat(self.context, now=NOW)
        daemon._heartbeat(self.context, now=NOW)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT heartbeat_at FROM leases WHERE lease_id=?', (self.lease,)).fetchone()[0], NOW)
            self.assertEqual(conn.execute("SELECT count(*) FROM daemon_events WHERE event_type='daemon_heartbeat'").fetchone()[0], 0)

    def test_daemon_keeps_bounded_diagnostics_and_returns_full_result(self):
        result = {'ok': False, 'status': 'failed', 'safe_error': 'failure ' * 1000, 'run_id': 'run-a', 'cycle_results': [{'run_id': 'run-b', 'status': 'failed', 'repo_summaries': ['x' * 10000]}], 'child': {'returncode': 1, 'stdout': 'x' * 100000, 'stderr': 'diagnostic'}}
        actual = daemon._run_live_poll(self.context, child_runner=lambda *a, **k: result, now=NOW)
        self.assertEqual(actual, result)
        with self.connect() as conn:
            raw = conn.execute("SELECT metadata_json FROM daemon_events WHERE status='failed'").fetchone()[0]
        payload = json.loads(raw)
        self.assertLess(len(raw), 5000)
        self.assertEqual(payload['run_id'], 'run-a')
        self.assertEqual(payload['run_ids'], ['run-b'])
        self.assertNotIn('stdout', payload.get('child', {}))
        self.assertTrue(payload['safe_error'].startswith('failure'))

    def test_summary_is_stored_once_and_cli_and_web_keep_full_output(self):
        self.add_run('run')
        summary = {'repo_summaries': [{'full_name': 'example/repo', 'status': 'completed'}], 'dispatch_count': 2}
        with self.connect() as conn:
            conn.execute('UPDATE agent_runs SET summary_json=? WHERE run_id=\'run\'', (json.dumps(summary),))
            run_once._mark_run_step(conn, 'run', 'summarize', 'succeeded', NOW, output=summary)
            raw = conn.execute("SELECT output_json FROM run_steps WHERE run_id='run'").fetchone()[0]
            self.assertNotIn('repo_summaries', raw)
            cli_run = status.build_run(conn, 'run')
        self.assertEqual(cli_run['steps'][0]['output'], summary)
        dashboard = web.build_dashboard_data(self.db)
        run = next(r for r in dashboard['recent_runs'] if r['run_id'] == 'run')
        self.assertEqual(run['steps'][0]['output'], summary)

    def test_prune_preserves_recovery_references_and_core_history(self):
        from robert_agent import retention
        for name in ('expired-success','source','consumed','leased'):
            self.add_run(name)
        self.add_run('expired-failure', 'failed')
        self.add_run('recent-failure', 'failed', RECENT_FAILURE)
        self.add_run('running', 'running', finished=False)
        self.add_run('unfinished', finished=False)
        self.add_run('latest', when=NOW)
        self.add_lease('active', 'active', 'leased')
        self.add_lease('old-release')
        self.add_lease('old-expired', 'expired')
        self.add_lease('running-owner', owner='running')
        self.add_event('old-event')
        self.add_event('recent-error', 'failed', RECENT_FAILURE)
        with self.connect() as conn:
            conn.execute("INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,source_run_id,consumed_run_id,status,not_before_at,created_at,updated_at) VALUES ('wake','repo','manual_operator_request','key','source','consumed','consumed',?,?,?)", (OLD,OLD,OLD))
        before = self.core_rows()
        planned = retention.prune_database(self.db, now=NOW)
        self.assertTrue(planned['ok'], planned)
        self.assertEqual(planned['counts']['agent_runs'], 2)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 9)
        applied = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertEqual(applied['counts'], planned['counts'])
        self.assertEqual(self.core_rows(), before)
        with self.connect() as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute("SELECT count(*) FROM leases WHERE lease_id IN ('active','running-owner')").fetchone()[0], 2)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM daemon_events WHERE daemon_event_id='recent-error'").fetchone())
        again = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertEqual(sum(again['counts'].values()), 0)

    def test_summary_reference_reads_completed_summary_during_concurrent_commit(self):
        self.add_run('run', 'running', finished=False)
        summary = {'dispatch_count': 3, 'repo_summaries': [{'status': 'completed'}]}
        real_connect = sqlite3.connect
        db = self.db

        class CompletingConnection(sqlite3.Connection):
            completed = False

            def execute(self, sql, parameters=()):
                if 'FROM run_steps' in sql and not self.completed:
                    self.completed = True
                    with closing(real_connect(db)) as writer, writer:
                        writer.execute("UPDATE agent_runs SET status='completed', summary_json=? WHERE run_id='run'", (json.dumps(summary),))
                        run_once._mark_run_step(writer, 'run', 'summarize', 'succeeded', NOW, output=summary)
                return super().execute(sql, parameters)

        for reader in ('cli', 'web'):
            with self.subTest(reader=reader):
                with self.connect() as conn:
                    conn.execute("UPDATE agent_runs SET status='running', summary_json='{}' WHERE run_id='run'")
                    conn.execute("UPDATE run_steps SET status='running', output_json='{}' WHERE run_id='run'")
                with closing(real_connect(db, factory=CompletingConnection)) as conn:
                    conn.row_factory = sqlite3.Row
                    if reader == 'cli':
                        run = status.build_run(conn, 'run')
                    else:
                        with mock.patch('robert_agent.web.sqlite3.connect', side_effect=lambda *a, **k: real_connect(*a, **k, factory=CompletingConnection)):
                            run = web.build_dashboard_data(db)['recent_runs'][0]
                    self.assertEqual(run['steps'][0]['status'], 'succeeded')
                    self.assertEqual(run['steps'][0]['output'], summary)
                    self.assertNotIn('referenced_summary_json', run['steps'][0])

    def test_batches_bound_work_and_timestamp_offsets_are_compared_as_instants(self):
        from robert_agent import retention
        for i in range(5):
            self.add_run('old-'+str(i))
        self.add_run('latest', when=NOW)
        self.add_run('boundary', when='2026-10-16T00:00:00Z')
        self.add_run('recent-offset', when='2026-10-15T23:30:00-02:00')
        first = retention.prune_database(self.db, now=NOW, apply=True, batch_size=2)
        self.assertEqual(first['counts']['agent_runs'], 2)
        second = retention.prune_database(self.db, now=NOW, apply=True, batch_size=2, max_batches=3)
        self.assertEqual(second['counts']['agent_runs'], 3)
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 3)

    @mock.patch("robert_agent.daemon.loop_engine.has_runnable_local_work", return_value=False)
    def test_maintenance_runs_again_in_resident_daemon_and_can_be_disabled(self, _local_work):
        self.add_run('old')
        self.add_run('latest', when=NOW)
        with mock.patch('robert_agent.daemon.time.monotonic', return_value=1):
            daemon.run_once_decision(self.context, now=NOW)
        with self.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM agent_runs WHERE run_id='old'").fetchone())
        self.add_run('another-old')
        with mock.patch('robert_agent.daemon.time.monotonic', return_value=2):
            daemon.run_once_decision(self.context, now=NOW)
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM agent_runs WHERE run_id='another-old'").fetchone())
        with mock.patch('robert_agent.daemon.time.monotonic', return_value=10000):
            daemon.run_once_decision(self.context, now=NOW)
        with self.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM agent_runs WHERE run_id='another-old'").fetchone())
        self.add_run('disabled')
        self.context.daemon_config['maintenance_enabled'] = False
        with mock.patch('robert_agent.daemon.time.monotonic', return_value=20000):
            daemon.run_once_decision(self.context, now=NOW)
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM agent_runs WHERE run_id='disabled'").fetchone())

    def test_busy_database_rolls_back_and_is_retryable(self):
        from robert_agent import retention
        self.add_run('old')
        self.add_run('latest', when=NOW)
        with closing(sqlite3.connect(self.db)) as lock:
            lock.execute('BEGIN IMMEDIATE')
            result = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'busy')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 2)

    def test_missing_database_is_not_created(self):
        from robert_agent import retention
        path = self.db.with_name('missing.sqlite3')
        for apply in (False, True):
            result = retention.prune_database(path, apply=apply, now=NOW)
            self.assertFalse(result['ok'])
        self.assertFalse(path.exists())

    def test_cli_defaults_to_read_only_then_applies_bounded_batches(self):
        from robert_agent.cli.main import main
        self.add_run('old-a')
        self.add_run('old-b')
        self.add_run('latest', when=NOW)
        validated = {'ok': True, 'db_path': str(self.db), 'daemon': dict(DAEMON_DEFAULTS)}
        before = self.db.read_bytes()
        with mock.patch('robert_agent.cli.main.validate_config.validate_config', return_value=validated) as validate:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['maintenance', 'prune', '--output', 'json', '--batch-size', '1'])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())['dry_run'])
            validate.assert_called_once_with(mock.ANY, skip_external=True)
        self.assertEqual(self.db.read_bytes(), before)
        with mock.patch('robert_agent.cli.main.validate_config.validate_config', return_value=validated):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['maintenance', 'prune', '--apply', '--output', 'json', '--batch-size', '1', '--batches', '2'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['counts']['agent_runs'], 2)

    def test_invalid_limits_cannot_delete_data(self):
        from robert_agent import retention
        for kwargs in ({'batch_size': 0}, {'batch_size': 501}, {'batch_size': True},
                       {'max_batches': 0}, {'run_retention_days': -1},
                       {'now': '2026-10-23T00:00:00'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                retention.prune_database(self.db, apply=True, **kwargs)

    def test_failed_batch_rolls_back_already_deleted_steps(self):
        from robert_agent import retention
        self.add_run('old')
        self.add_run('latest', when=NOW)
        with self.connect() as conn:
            conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON run_repo_steps BEGIN SELECT RAISE(ABORT,'blocked for test'); END")
        result = retention.prune_database(self.db, now=NOW, apply=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'failed')
        with self.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM run_steps').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM agent_runs').fetchone()[0], 2)

    def test_reference_and_legacy_summary_have_identical_compact_cli_output(self):
        self.add_run('run')
        summary = {'items': ['x' * 1000] * 20, **{str(i): i for i in range(12)}}
        with self.connect() as conn:
            conn.execute("UPDATE agent_runs SET summary_json=? WHERE run_id='run'", (json.dumps(summary),))
            conn.execute("UPDATE run_steps SET output_json=? WHERE run_id='run'", (json.dumps(summary),))
            legacy = status.build_run(conn, 'run')
            run_once._mark_run_step(conn, 'run', 'summarize', 'succeeded', NOW, output=summary)
            current = status.build_run(conn, 'run')
            self.assertEqual(current['steps'][0]['output'], legacy['steps'][0]['output'])

    def test_child_diagnostic_secret_is_omitted(self):
        result = {'ok': False, 'safe_error': 'Authorization: sensitive', 'status': 'failed'}
        daemon._run_local_drain(self.context, child_runner=lambda *a, **k: result, now=NOW)
        with self.connect() as conn:
            raw = conn.execute("SELECT metadata_json FROM daemon_events WHERE status='failed'").fetchone()[0]
        self.assertNotIn('Authorization:', raw)
        self.assertIn('omitted', raw)

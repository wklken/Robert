from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3
import hashlib
import tracemalloc
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import model_execution, review_files, review_store, storage, vote_store
from robert_agent.worker import heartbeat, snapshot
from tests import test_review_coordinator as coordinator_fixture


class RuntimeRemediationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / 'runtime.sqlite3'
        storage.init_database(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','example/r','bot','main','/repo','/worktrees')")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('w','r','active','2026-10-26','2026-10-26')")
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,priority,created_at,updated_at) VALUES ('t','w','running','P1','2026-10-26','2026-10-26')")
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a','t',1,'running')")

    def test_command_completion_does_not_wait_for_heartbeat_deadline(self):
        started = time.monotonic()
        result = heartbeat.run_command_with_heartbeat(
            self.db, 't', 'a', 'verify',
            [sys.executable, '-c', "import time; time.sleep(.02); print('done')"],
            heartbeat_interval_seconds=1.5, timeout_seconds=3,
        )
        self.assertTrue(result['ok'])
        self.assertIn('done', result['stdout_tail'])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_identical_snapshots_refresh_liveness_without_duplicating_history(self):
        stamps = [datetime.fromisoformat(f'2026-10-26T00:00:0{second}+00:00') for second in range(1, 4)]
        with patch.object(snapshot, 'datetime') as clock:
            clock.now.side_effect = stamps
            for _ in range(3):
                snapshot.record_snapshot(self.db, 't', 'a', 'verify', 'running',
                                         'Command still running', 'Wait for command completion')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM worker_phases').fetchone()[0], 1)
            actual = conn.execute("SELECT heartbeat_at FROM attempts WHERE attempt_id='a'").fetchone()[0]
            self.assertEqual(actual, stamps[-1].isoformat())
        snapshot.record_snapshot(self.db, 't', 'a', 'verify', 'completed', 'Command exited 0')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM worker_phases').fetchone()[0], 2)

    def test_duplicate_snapshot_does_not_bypass_task_ownership(self):
        snapshot.record_snapshot(self.db, 't', 'a', 'verify', 'running', 'Working')
        with self.assertRaisesRegex(ValueError, 'does not belong'):
            snapshot.record_snapshot(self.db, 'other-task', 'a', 'verify', 'running', 'Working')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM worker_phases').fetchone()[0], 1)

    def test_activity_queries_do_not_scan_completed_history(self):
        with closing(sqlite3.connect(self.db)) as conn:
            for table in ('review_runs', 'vote_runs'):
                plan = conn.execute(f'EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE slot_reserved=1').fetchall()
                self.assertTrue(any('USING INDEX' in row[3] for row in plan), (table, plan))
            plan = conn.execute("""EXPLAIN QUERY PLAN SELECT phase,status,summary,next_step
                FROM worker_phases WHERE attempt_id='a'
                ORDER BY created_at DESC,phase_id DESC LIMIT 1""").fetchall()
            self.assertTrue(any('USING INDEX' in row[3] for row in plan), plan)

    def test_legacy_model_tables_upgrade_before_activity_index_creation(self):
        for module in (review_store, vote_store):
            with self.subTest(store=module.__name__), closing(sqlite3.connect(':memory:')) as conn:
                legacy = module.SCHEMA.replace('  model TEXT,\n', '').replace(
                    '  slot_reserved INTEGER NOT NULL DEFAULT 0,\n', '').replace('  started_at TEXT,\n', '')
                conn.executescript(legacy)
                module.initialize(conn)
                table = 'review_runs' if module is review_store else 'vote_runs'
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                self.assertTrue({'model', 'slot_reserved', 'started_at'} <= columns)
                plan = conn.execute(f'EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE slot_reserved=1').fetchall()
                self.assertTrue(any('USING INDEX' in row[3] for row in plan), plan)

    def test_file_hash_preserves_digest_with_bounded_memory(self):
        path = self.root / 'large.bin'
        block = b'x' * (1024 * 1024)
        expected = hashlib.sha256()
        with path.open('wb') as stream:
            for _ in range(16):
                stream.write(block)
                expected.update(block)
        tracemalloc.start()
        try:
            actual = review_files.sha256_file(path)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(actual, expected.hexdigest())
        self.assertLess(peak, 4 * 1024 * 1024)


class CoordinatorHeartbeatRemediationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = coordinator_fixture.ReviewCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.cycle([self.fixture.event()])
        self.driver = self.fixture.coordinator()

    def test_repeated_activity_checks_coalesce_parent_heartbeat(self):
        timestamps = [f'2026-10-26T00:00:0{second}+00:00' for second in range(1, 7)]
        with patch.object(review_store, 'now', side_effect=timestamps), patch.object(model_execution.time, 'monotonic', return_value=1):
            self.driver.check_active()
            self.driver.check_active()
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: self.driver.check_active(), range(4)))
        with review_store.connect(self.fixture.db) as conn:
            actual = conn.execute('SELECT heartbeat_at FROM attempts WHERE attempt_id=?', (self.driver.attempt_id,)).fetchone()[0]
        self.assertEqual(actual, timestamps[0])

    def test_parent_heartbeat_refreshes_after_coalescing_interval(self):
        timestamps = ['2026-10-26T00:00:01+00:00', '2026-10-26T00:00:31+00:00']
        with patch.object(review_store, 'now', side_effect=timestamps), patch.object(model_execution.time, 'monotonic', side_effect=[1, 31]):
            self.driver.check_active()
            self.driver.check_active()
        with review_store.connect(self.fixture.db) as conn:
            actual = conn.execute('SELECT heartbeat_at FROM attempts WHERE attempt_id=?', (self.driver.attempt_id,)).fetchone()[0]
        self.assertEqual(actual, timestamps[-1])

    def test_busy_heartbeat_write_does_not_fail_active_waiter(self):
        with closing(sqlite3.connect(self.fixture.db)) as writer, writer:
            writer.execute('BEGIN IMMEDIATE')
            self.driver.check_active()
        with review_store.connect(self.fixture.db) as conn:
            conn.execute("UPDATE execution_subjects SET binding_status='attention' WHERE subject_type='task' AND subject_id=?", (self.driver.task_id,))
        with self.assertRaises(model_execution.ReviewStopped):
            self.driver.check_active()

    def test_cancellation_is_checked_even_when_heartbeat_is_not_due(self):
        self.driver.check_active()
        with review_store.connect(self.fixture.db) as conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (self.driver.task_id,))
        with self.assertRaises(model_execution.ReviewStopped):
            self.driver.check_active()

    def test_non_contention_heartbeat_errors_remain_visible(self):
        original_connect = sqlite3.connect
        class BrokenHeartbeatConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith('UPDATE attempts SET heartbeat_at'):
                    raise sqlite3.OperationalError('disk I/O error')
                return super().execute(sql, parameters)
        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=BrokenHeartbeatConnection)
        with patch.object(sqlite3, 'connect', side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk I/O error'):
                self.driver.check_active()

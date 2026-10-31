"""Native session history must not pin isolated, proven-stopped processes."""
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from robert_agent import native_rounds, result_cutover, run_once, supervise, worker_identity
from robert_agent.publication_lock import task_publication_lock
from tests import test_cutover_recovery as cutover_fixture
from tests import test_review_coordinator as review_fixture
from tests import test_vote_coordinator as vote_fixture


EXITED = {'ok': True, 'pid': 1073741824, 'process_exited': True, 'returncode': 0}


class NativeCutoverRecoveryTests(unittest.TestCase):
    def fixture(self, store):
        fixture = cutover_fixture.CutoverRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with store.connect(fixture.db) as conn:
            entry = store.enqueue(conn, {'full_name': 'x/y'}, {
                'number': 205, 'source_type': 'issue', 'workstream_id': 'bad',
                'event_fingerprint': 'native-cutover'}, {}, [])
            store.update(conn, entry['round_id'], task_id='bad', status='ready')
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id='bad'",
                         (json.dumps({'result_contract': 'legacy-v0', 'dispatch': EXITED}),))
        return fixture, entry['round_id']

    def run_record(self, fixture, store, round_id, *, status='running', reserved=1, dispatch=EXITED):
        with store.connect(fixture.db) as conn:
            run = store.new_run(conn, round_id, 'prepare', 'model', 'bad',
                                fixture.tmp.name, 'prompt-sha')
            store.update_run(conn, run['run_id'], status=status, slot_reserved=reserved,
                             dispatch_json=json.dumps(dispatch))
        return run['run_id']

    def state(self, fixture, store, round_id):
        with store.connect(fixture.db) as conn:
            parent = dict(conn.execute("SELECT * FROM attempts WHERE attempt_id='bad'").fetchone())
            occupied = conn.execute("SELECT COUNT(*) FROM attempts a WHERE task_id='bad' AND "
                                    + supervise.occupied_attempt_sql()).fetchone()[0]
            return parent, occupied, store.runs(conn, round_id)

    def observe(self, fixture):
        with patch.object(worker_identity, 'terminate_recorded_identity') as terminate, \
             patch.object(run_once, '_terminate_attempt_process') as stop_parent, \
             patch.object(native_rounds, 'terminate_children') as stop_children:
            result = result_cutover.prepare(fixture.db, now=fixture.now)
            self.assertTrue(result['ok'], result)
            terminate.assert_not_called()
            stop_parent.assert_not_called()
            stop_children.assert_not_called()

    def test_historical_review_and_vote_results_release_dead_parent(self):
        for fixture_type in (review_fixture.ReviewCoordinatorTests, vote_fixture.VoteCoordinatorTests):
            for status in ('running', 'completed'):
                with self.subTest(workflow=fixture_type.__name__, parent=status):
                    fixture = fixture_type()
                    fixture.setUp()
                    self.addCleanup(fixture.doCleanups)
                    if fixture_type is review_fixture.ReviewCoordinatorTests:
                        fixture.cycle([fixture.event()])
                        driver = fixture.coordinator()
                    else:
                        driver = fixture.start()
                    report = driver.run()
                    report_bytes = report.read_bytes()
                    identity = self.process_identity(stopped=True)
                    with driver.store.connect(fixture.db) as conn:
                        results = [tuple(row) for row in conn.execute('SELECT * FROM worker_results')]
                        before_runs = driver.store.runs(conn, driver.round_id)
                        before_session = driver.store.session(conn, task_id=driver.task_id)
                        self.assertEqual(len(before_runs), 4)
                        self.assertTrue(all(run['status'] == 'succeeded' and not run['slot_reserved']
                                            for run in before_runs))
                        conn.execute("UPDATE attempts SET status=?,finished_at='original',metadata_json="
                                     "json_set(metadata_json,'$.result_contract','legacy-v0','$.dispatch',json(?)) "
                                     "WHERE attempt_id=?", (status, json.dumps(identity), driver.attempt_id))
                        # Completed parents already carry the historical isolation marker.
                        if status == 'completed':
                            conn.execute("UPDATE tasks SET metadata_json=json_set(metadata_json,"
                                         "'$.execution_attention',json(?)) WHERE task_id=?",
                                         (json.dumps({'reason_code': 'result_contract_cutover'}), driver.task_id))
                    for _ in range(2):
                        result = result_cutover.prepare(fixture.db)
                        self.assertTrue(result['ok'], result)
                    with driver.store.connect(fixture.db) as conn:
                        parent = conn.execute('SELECT * FROM attempts WHERE attempt_id=?', (driver.attempt_id,)).fetchone()
                        self.assertEqual(parent['status'], 'failed' if status == 'running' else 'completed')
                        self.assertTrue(json.loads(parent['metadata_json']).get('process_stopped_at'))
                        if status == 'completed':
                            self.assertEqual(parent['finished_at'], 'original')
                        self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts a WHERE '
                                                      + supervise.occupied_attempt_sql()).fetchone()[0], 0)
                        self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM worker_results')], results)
                        self.assertEqual(driver.store.runs(conn, driver.round_id), before_runs)
                        self.assertEqual(driver.store.session(conn, task_id=driver.task_id), before_session)
                    self.assertEqual(report.read_bytes(), report_bytes)

    def process_identity(self, *, stopped=False):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        identity = worker_identity.capture_process_identity(process.pid)
        if stopped:
            cleanup()
        return identity

    def test_releases_only_proven_stopped_slots_while_unknown_child_retains_parent(self):
        for store in native_rounds.STORES:
            with self.subTest(store=store.__name__):
                fixture, round_id = self.fixture(store)
                self.run_record(fixture, store, round_id)
                unknown_id = self.run_record(fixture, store, round_id, status='starting', dispatch={})
                self.observe(fixture)
                parent, occupied, runs = self.state(fixture, store, round_id)
                self.assertEqual((parent['status'], occupied), ('running', 1))
                self.assertEqual({r['run_id']: r['slot_reserved'] for r in runs},
                                 {r['run_id']: int(r['run_id'] == unknown_id) for r in runs})

    def test_stopped_reserved_model_releases_parent_and_slot_without_changing_run_result(self):
        for store in native_rounds.STORES:
            for status in ('running', 'failed', 'succeeded', 'superseded'):
                with self.subTest(store=store.__name__, status=status):
                    fixture, round_id = self.fixture(store)
                    self.run_record(fixture, store, round_id, status=status,
                                    dispatch=self.process_identity(stopped=True))
                    before = self.state(fixture, store, round_id)[2][0]
                    self.observe(fixture)
                    parent, occupied, runs = self.state(fixture, store, round_id)
                    self.assertEqual((parent['status'], occupied), ('failed', 0))
                    self.assertEqual(runs[0], {**before, 'slot_reserved': 0})

    def test_unknown_live_and_unsafe_group_identity_retain_capacity(self):
        live = self.process_identity()
        dead = self.process_identity(stopped=True)
        cases = [({}, 'starting'), ({}, 'running'), ({'pid': dead['pid']}, 'failed'),
                 (live, 'running'), ({**live, 'process_start_token': 'different'}, 'running'),
                 ({**dead, 'process_group_id': live['pid']}, 'running'),
                 ({'ok': False, 'cleanup_pending': True}, 'failed')]
        for store in native_rounds.STORES:
            for dispatch, status in cases:
                with self.subTest(store=store.__name__, dispatch=dispatch, status=status):
                    fixture, round_id = self.fixture(store)
                    self.run_record(fixture, store, round_id, status=status, dispatch=dispatch)
                    self.observe(fixture)
                    parent, occupied, runs = self.state(fixture, store, round_id)
                    self.assertEqual((parent['status'], occupied, runs[0]['slot_reserved']), ('running', 1, 1))

    def test_prelaunch_and_known_failed_start_do_not_pin_parent(self):
        cases = [('prepared', 0, {}), ('waiting', 0, {}), ('interrupted', 0, {}),
                 ('abandoned', 0, {}), ('failed', 1, {'ok': False, 'status': 'failed_dispatch', 'pid': None})]
        for store in native_rounds.STORES:
            for status, reserved, dispatch in cases:
                with self.subTest(store=store.__name__, status=status):
                    fixture, round_id = self.fixture(store)
                    self.run_record(fixture, store, round_id, status=status, reserved=reserved, dispatch=dispatch)
                    self.observe(fixture)
                    parent, occupied, runs = self.state(fixture, store, round_id)
                    self.assertEqual((parent['status'], occupied, runs[0]['slot_reserved']), ('failed', 0, 0))

    def test_child_changes_during_read_only_observation_invalidate_parent_and_slot_updates(self):
        for store, table in zip(native_rounds.STORES, ('review_runs', 'vote_runs')):
            for change in ('dispatch', 'owner', 'session', 'insert'):
                with self.subTest(store=store.__name__, change=change):
                    fixture, round_id = self.fixture(store)
                    identity = self.process_identity(stopped=True)
                    run_id = self.run_record(fixture, store, round_id, dispatch=identity)
                    original = run_once._dispatch_process_status
                    changed = []
                    def inspect(metadata):
                        if metadata['dispatch'].get('pid') == identity['pid'] and not changed:
                            # This writer must succeed while the OS observation runs.
                            with closing(sqlite3.connect(fixture.db, timeout=.05)) as conn, conn:
                                conn.execute('BEGIN IMMEDIATE')
                                if change == 'dispatch':
                                    conn.execute(f"UPDATE {table} SET dispatch_json='{{}}' WHERE run_id=?", (run_id,))
                                elif change == 'owner':
                                    conn.execute(f"UPDATE {table} SET owner_attempt_id='good' WHERE run_id=?", (run_id,))
                                elif change == 'session':
                                    conn.execute(f"UPDATE {table.replace('_runs', '_sessions')} SET status='running' WHERE round_id=?", (round_id,))
                                else:
                                    new = store.new_run(conn, round_id, 'prepare', 'other', 'bad', fixture.tmp.name, 'sha')
                                    store.update_run(conn, new['run_id'], status='starting', slot_reserved=1)
                            changed.append(True)
                        return original(metadata)
                    with patch.object(run_once, '_dispatch_process_status', side_effect=inspect):
                        self.observe(fixture)
                    self.assertTrue(changed)
                    parent, occupied, runs = self.state(fixture, store, round_id)
                    self.assertEqual((parent['status'], occupied), ('running', 1))
                    self.assertTrue(all(r['slot_reserved'] for r in runs))

    def test_task_lock_prevents_observation_during_launch_registration(self):
        for store in native_rounds.STORES:
            with self.subTest(store=store.__name__):
                fixture, round_id = self.fixture(store)
                self.run_record(fixture, store, round_id)
                with task_publication_lock(fixture.db, 'bad') as acquired:
                    self.assertTrue(acquired)
                    self.assertFalse(result_cutover.prepare(fixture.db, now=fixture.now)['ok'])
                    self.assertEqual(self.state(fixture, store, round_id)[0]['status'], 'running')
                self.observe(fixture)
                self.assertEqual(self.state(fixture, store, round_id)[0]['status'], 'failed')

    def test_dead_model_leader_with_live_descendant_retains_parent_and_slot(self):
        for store in native_rounds.STORES:
            with self.subTest(store=store.__name__):
                fixture, round_id = self.fixture(store)
                ready = Path(fixture.tmp.name) / 'child-ready'
                release = Path(fixture.tmp.name) / 'release-leader'
                code = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                        f"Path({str(ready)!r}).touch()\n"
                        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n")
                process = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    identity = worker_identity.capture_process_identity(process.pid)
                    release.touch()
                    process.wait(timeout=5)
                    self.run_record(fixture, store, round_id, dispatch=identity)
                    self.observe(fixture)
                    parent, occupied, runs = self.state(fixture, store, round_id)
                    self.assertEqual((parent['status'], occupied, runs[0]['slot_reserved']), ('running', 1, 1))
                finally:
                    # Test-owned group only; recovery itself has no signal authority.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

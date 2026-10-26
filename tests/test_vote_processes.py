from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
import unittest

from robert_agent import dispatch, native_rounds, vote_store, worker_identity
from tests import test_vote_coordinator as fixture_module


class VoteProcessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.VoteCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.driver = self.fixture.start()
        self.driver.poll_seconds = .02
        self.driver.prepare()
        self.gate = self.fixture.root / 'release'
        self.started = []
        self.mutex = threading.Lock()
        self.addCleanup(self.cleanup_processes)
        self.script = self.fixture.root / 'fake_voter.py'
        self.script.write_text('''import hashlib,json,os,pathlib,sys,time
root,manifest_path,worker,gate=map(pathlib.Path,sys.argv[1:5])
prompt=sys.stdin.buffer.read()
manifest=json.loads(manifest_path.read_text())
(root/'started').write_text(hashlib.sha256(prompt).hexdigest())
end=time.monotonic()+10
while not pathlib.Path(gate).exists():
    if time.monotonic()>end:sys.exit(12)
    time.sleep(.01)
value={k:manifest[k] for k in ('schema_version','round_id','snapshot_id','ballot_spec_sha256')}
value.update(choice_id='A',reason_summary='Vote from '+str(worker),evidence_refs=[],tradeoffs=[],uncertainties=[])
(root/'ballot.json.tmp').write_text(json.dumps(value))
(root/'ballot.json.tmp').replace(root/'ballot.json')
''')
        self.driver.launcher = self.launch

    def launch(self, **kwargs):
        output = Path(kwargs['trusted_environment']['ROBERT_VOTE_OUTPUT_DIR'])
        # Exercise both existing CLI adapters with a fake binary; extra adapter flags are ignored by the fixture.
        worker = dict(kwargs['worker_agent_config'])
        worker['command_argv'] = [sys.executable, str(self.script), str(output),
            str(Path(self.driver.manifest['context_dir']) / 'manifest.json'), worker['name'], str(self.gate)]
        kwargs['worker_agent_config'] = worker
        result = dispatch.dispatch_worker(**kwargs, startup_probe_seconds=.01)
        with self.mutex:
            self.started.append((result, output))
        return result

    def cleanup_processes(self):
        self.gate.touch()
        for record, _ in self.started:
            worker_identity.terminate_recorded_identity(record)
            proc = dispatch._BACKGROUND_PROCESSES.pop(record.get('pid'), None)
            if proc:
                proc.wait(timeout=5)

    def wait_started(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if sum((p / 'started').exists() for _, p in self.started) == 2:
                return
            time.sleep(.01)
        self.fail('both real voters did not overlap')

    def test_two_adapters_run_concurrently_with_identical_input_and_private_output(self):
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.driver.vote)
            try:
                self.wait_started()
                with vote_store.connect(self.fixture.db) as conn:
                    self.assertEqual(vote_store.process_pool_status(conn)['in_use'], 2)
                self.assertEqual(len({(p / 'started').read_text() for _, p in self.started}), 1)
                self.assertEqual(len({p for _, p in self.started}), 2)
            finally:
                self.gate.touch()
            records = future.result(timeout=10)
        self.assertEqual(len(records), 2)
        with vote_store.connect(self.fixture.db) as conn:
            self.assertEqual(vote_store.process_pool_status(conn)['in_use'], 0)
        self.assertEqual({r['adapter'] for r in records}, {'codex','claude'})

    def test_cancel_stops_both_registered_voters_and_does_not_seal_votes(self):
        from robert_agent.model_execution import ReviewStopped
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(self.driver.vote)
            try:
                self.wait_started()
                self.driver.stopping.set()
                with vote_store.connect(self.fixture.db) as conn:
                    self.assertNotEqual(native_rounds.terminate_children(conn, self.driver.task_id), 'attention')
                with self.assertRaises(ReviewStopped):
                    future.result(timeout=10)
            finally:
                self.gate.touch()
        with vote_store.connect(self.fixture.db) as conn:
            self.assertFalse(any(r['status'] == 'succeeded' and r['stage'] == 'vote'
                                 for r in vote_store.runs(conn, self.driver.round_id)))
            self.assertEqual(vote_store.process_pool_status(conn)['in_use'], 0)

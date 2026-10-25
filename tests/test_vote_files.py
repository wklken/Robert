import copy
import os
from pathlib import Path
import tempfile
import unittest

from robert_agent import vote_files, vote_workspace, vote_coordinator, vote_store, model_pool, review_store
from tests import test_vote_coordinator as fixture_module


class VoteFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        context = self.root / 'context'
        context.mkdir()
        self.manifest = {'schema_version': 1, 'round_id': 'vote-test', 'snapshot_id': 'snapshot',
            'artifact_root': str(self.root), 'context_dir': str(context), 'worktree_path': str(context),
            'workers': [{'name': 'one'}, {'name': 'two'}], 'sources': {'source-1': {'body': 'decision'}},
            'files': {}, 'frozen': False, 'source_type': 'issue'}
        vote_files.atomic_json(context / 'manifest.json', self.manifest)
        self.prepared = {'schema_version': 1, 'round_id': 'vote-test', 'snapshot_id': 'snapshot',
            'status': 'ready', 'question': 'Which one?', 'criteria': ['Reliability'],
            'options': [{'id': 'A', 'description': 'A', 'origin': 'user'}, {'id': 'B', 'description': 'B', 'origin': 'user'}]}
        self.manifest = vote_workspace.freeze(self.manifest, self.prepared)
        self.ballot = {k: self.manifest[k] for k in ('schema_version', 'round_id', 'snapshot_id', 'ballot_spec_sha256')}
        self.ballot.update(choice_id='A', reason_summary='Public reason', evidence_refs=['source-1'], tradeoffs=[], uncertainties=[])
        self.path = self.root / 'ballot.json'

    def read(self, value):
        vote_files.atomic_json(self.path, value)
        return vote_files.read_ballot(self.path, self.manifest)

    def test_valid_vote_and_explicit_abstention(self):
        self.assertEqual(self.read(self.ballot)['choice_id'], 'A')
        self.assertIsNone(self.read({**self.ballot, 'choice_id': None, 'abstain_reason': 'Need evidence'})['choice_id'])

    def test_invalid_identity_options_missing_reasons_and_unknown_evidence(self):
        for mutation in [{'round_id': 'old'}, {'snapshot_id': 'old'}, {'ballot_spec_sha256': 'old'},
                         {'choice_id': 'C'}, {'choice_id': True}, {'choice_id': []},
                         {'reason_summary': ''}, {'reason_summary': 'x'*1501},
                         {'choice_id': None}, {'evidence_refs': ['unknown']}]:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.read({**self.ballot, **mutation})

    def test_changed_prompt_or_spec_cannot_be_used(self):
        self.assertTrue(vote_workspace.verify_snapshot(self.manifest))
        Path(self.manifest['prompt_path']).write_text('another question')
        with self.assertRaises(ValueError):
            vote_workspace.verify_snapshot(self.manifest)

    def test_selected_ballots_require_full_coverage_and_unchanged_hash(self):
        self.read(self.ballot)
        records = [{'worker_id': w, 'ballot_path': str(self.path), 'ballot_sha256': vote_files.digest(self.path)} for w in ('one','two')]
        self.assertEqual(len(vote_files.selected_ballots(self.manifest, records)), 2)
        for invalid in [records[:1], [records[0], records[0]], list(reversed(records))]:
            with self.assertRaises(ValueError):
                vote_files.selected_ballots(self.manifest, invalid)
        self.path.write_text('{}')
        with self.assertRaises(ValueError):
            vote_files.selected_ballots(self.manifest, records)

    def test_prepare_rejects_duplicate_or_missing_options(self):
        for changes in [{'options': []}, {'options': [self.prepared['options'][0]] * 2}, {'criteria': []}]:
            value = {**self.prepared, **changes}
            vote_files.atomic_json(self.path, value)
            with self.assertRaises(ValueError):
                vote_files.read_prepared(self.path, self.manifest)

    def test_seal_rejects_symlink_and_fifo(self):
        driver = object.__new__(vote_coordinator.Coordinator)
        driver.manifest = self.manifest
        output = self.root / 'output'
        output.mkdir()
        run = {'stage': 'vote', 'run_id': 'vote-run-test', 'output_dir': str(output), 'prompt_sha256': 'hash'}
        target = output / 'ballot.json'
        target.symlink_to(self.path)
        with self.assertRaises(OSError):
            driver.sealed_output(run)
        target.unlink()
        os.mkfifo(target)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            driver.sealed_output(run)

    def test_freshness_ignores_new_comments_but_rejects_edits_and_deleted_sources(self):
        manifest = {**self.manifest, 'sources': {'source-1': {'kind': 'target', 'title': 'Decision', 'body': 'A/B'},
                  'comment:1': {'kind': 'comment', 'id': 1, 'body': 'Compare'}}}
        current = {'state': 'open', 'title': 'Decision', 'body': 'A/B', 'discussion': [{'id': 1, 'body': 'Compare'}]}
        self.assertTrue(vote_workspace.same_sources(manifest, current))
        newer = copy.deepcopy(current)
        newer['discussion'].append({'id': 2, 'body': 'New unrelated comment'})
        self.assertTrue(vote_workspace.same_sources(manifest, newer))
        newer['discussion'][0]['body'] = 'Edited decision'
        self.assertFalse(vote_workspace.same_sources(manifest, newer))
        self.assertFalse(vote_workspace.same_sources(manifest, {**current, 'discussion': []}))


class VotePoolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.VoteCoordinatorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.driver = self.fixture.start()
        self.driver.prepare()

    def test_review_and_vote_reservations_share_one_atomic_limit(self):
        with vote_store.connect(self.fixture.db) as conn:
            review_store.configure_process_pool(conn, 1)
            review_store.enqueue(conn, self.driver.request['repo'], self.driver.request['event'], {}, [])
            review = conn.execute('SELECT round_id FROM review_sessions').fetchone()[0]
            r = review_store.new_run(conn, review, 'review', 'codex', self.driver.attempt_id,
                                     self.fixture.root / 'review', 'hash')
            v = vote_store.new_run(conn, self.driver.round_id, 'vote', 'codex', self.driver.attempt_id,
                                   self.driver.manifest['artifact_root'], 'hash')
            for store, run in [(review_store, r), (vote_store, v)]:
                store.update_run(conn, run['run_id'], status='waiting')
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            self.assertTrue(review_store.reserve_slot(conn, r['run_id']))
            self.assertFalse(vote_store.reserve_slot(conn, v['run_id']))
            self.assertEqual(model_pool.status(conn), {'limit': 1, 'in_use': 1})
            review_store.release_slot(conn, r['run_id'])
            self.assertTrue(vote_store.reserve_slot(conn, v['run_id']))
            self.assertEqual(review_store.process_pool_status(conn), {'limit': 1, 'in_use': 1})
            self.assertEqual(vote_store.process_pool_status(conn), {'limit': 1, 'in_use': 1})

    def test_unknown_vote_launch_retains_slot_and_blocks_restart(self):
        with vote_store.connect(self.fixture.db) as conn:
            run = vote_store.new_run(conn, self.driver.round_id, 'vote', 'codex', self.driver.attempt_id,
                                     self.driver.manifest['artifact_root'], 'hash')
            vote_store.update_run(conn, run['run_id'], status='starting', slot_reserved=1)
        from robert_agent.model_execution import ReviewStopped
        with self.assertRaises(ReviewStopped):
            self.fixture.driver().run()
        with vote_store.connect(self.fixture.db) as conn:
            self.assertEqual(model_pool.status(conn)['in_use'], 1)
            self.assertEqual(vote_store.session(conn, task_id=self.driver.task_id)['status'], 'attention')

    def test_vote_reclaims_a_confirmed_dead_review_group_but_not_an_unknown_start(self):
        with vote_store.connect(self.fixture.db) as conn:
            review_store.configure_process_pool(conn, 1)
            entry = review_store.enqueue(conn, self.driver.request['repo'], self.driver.request['event'], {}, [])
            run = review_store.new_run(conn, entry['round_id'], 'review', 'codex', self.driver.attempt_id,
                                       self.fixture.root / 'review-orphan', 'hash')
            import json
            review_store.update_run(conn, run['run_id'], status='running', slot_reserved=1,
                dispatch_json=json.dumps({'pid': 1073741824, 'process_group_id': 1073741824}))
        self.driver.reclaim_finished_slots()
        with vote_store.connect(self.fixture.db) as conn:
            self.assertEqual(model_pool.status(conn)['in_use'], 0)
            review_store.update_run(conn, run['run_id'], status='starting', slot_reserved=1, dispatch_json='{}')
        self.driver.reclaim_finished_slots()
        with vote_store.connect(self.fixture.db) as conn:
            self.assertEqual(model_pool.status(conn)['in_use'], 1)

"""Cutover checks scale with history while preserving live blocker detection."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent import result_contract, result_cutover, storage


class CutoverScalingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def history(self, count):
        path = self.root / f'history-{count}.sqlite'
        storage.init_database(path)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','x/y','bot','main','/repo','/trees')")
            conn.executemany("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES(?,'repo','completed','now','now')",
                             [(f'w{i}',) for i in range(count)])
            conn.executemany("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,'completed','now','now')",
                             [(f't{i}', f'w{i}') for i in range(count)])
            conn.executemany("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES(?,?,1,'completed')",
                             [(f'a{i}', f't{i}') for i in range(count)])
            conn.executemany("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES(?,?,?,'classification_result','now')",
                             [(f'r{i}', f't{i}', f'a{i}') for i in range(count)])
            conn.executemany("INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at) VALUES(?,?,?,'comment','accepted','published','now')",
                             [(f'g{i}', f'r{i}', f't{i}') for i in range(count)])
        return path

    def check_steps(self, path):
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return 0

        with closing(sqlite3.connect(path)) as conn:
            conn.set_progress_handler(progress, 100)
            try:
                checked = result_cutover.inspect(conn, path)
            finally:
                conn.set_progress_handler(None, 0)
        self.assertTrue(checked['ok'], checked)
        return steps

    def test_doubling_terminal_history_does_not_quadruple_database_work(self):
        # SQLite VM steps are deterministic work evidence, independent of machine speed.
        small = self.check_steps(self.history(200))
        large = self.check_steps(self.history(400))
        self.assertLess(large, small * 3, (small, large))

    def test_later_unresolved_legacy_state_blocks_without_cached_readiness(self):
        path = self.history(3)
        self.assertTrue(result_cutover.check(path)['ok'])
        mutations = [
            ("UPDATE tasks SET lifecycle='queued' WHERE task_id='t1'", "UPDATE tasks SET lifecycle='completed' WHERE task_id='t1'"),
            ("UPDATE workstreams SET lifecycle='active',active_task_id='t1' WHERE workstream_id='w1'", "UPDATE workstreams SET lifecycle='completed',active_task_id=NULL WHERE workstream_id='w1'"),
            ("UPDATE github_actions SET publish_status='not_published' WHERE action_id='g1'", "UPDATE github_actions SET publish_status='published' WHERE action_id='g1'"),
        ]
        for activate, deactivate in mutations:
            with self.subTest(mutation=activate):
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute(activate)
                checked = result_cutover.check(path)
                self.assertFalse(checked['ok'], checked)
                self.assertIn({'kind': 'legacy_unresolved_result', 'task_id': 't1', 'result_id': 'r1'}, checked['blockers'])
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute(deactivate)
                self.assertTrue(result_cutover.check(path)['ok'])

    def test_current_contract_is_allowed_even_with_pending_publication(self):
        path = self.history(1)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='not_published'")
            conn.execute("UPDATE worker_results SET metadata_json=json_object('result_contract',?)", (result_contract.CONTRACT_ID,))
        self.assertTrue(result_cutover.check(path)['ok'])


if __name__ == '__main__':
    unittest.main()

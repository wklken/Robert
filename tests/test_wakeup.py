from tests import support  # noqa: F401
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage, wakeup


class WakeupReasonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'state.sqlite3'
        storage.init_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('repo','example/repo','bot','main','/repo','/worktrees')")

    def test_machine_wakeup_serializes_logical_reason_without_changing_identity(self):
        for kind in ('github_observation_ready', 'github_context_refresh'):
            with self.subTest(kind=kind):
                created = wakeup.request_wakeup(self.conn, repo_id='repo',
                    reason='manual_operator_request', dedupe_key=kind, metadata={'kind': kind})
                self.assertEqual(created['reason'], kind)
                duplicate = wakeup.request_wakeup(self.conn, repo_id='repo',
                    reason='manual_operator_request', dedupe_key=kind, metadata={'kind': kind})
                self.assertEqual(duplicate['reason'], kind)
                self.assertEqual(duplicate['wakeup_id'], created['wakeup_id'])
                self.assertEqual(duplicate['status'], 'already_pending')
                stored_reason, stored_dedupe = self.conn.execute(
                    'SELECT reason,dedupe_key FROM wakeups WHERE wakeup_id=?', (created['wakeup_id'],)).fetchone()
                self.assertEqual((stored_reason, stored_dedupe), ('manual_operator_request', kind))
        rows = wakeup.list_wakeups(self.conn)
        self.assertEqual({row['reason'] for row in rows}, {'github_observation_ready', 'github_context_refresh'})
        self.conn.row_factory = sqlite3.Row
        self.assertEqual({row['reason'] for row in wakeup.list_wakeups(self.conn)},
                         {'github_observation_ready', 'github_context_refresh'})

    def test_statistics_keep_machine_and_manual_requests_separate(self):
        for kind in ('github_observation_ready', 'github_context_refresh', 'operator_decision'):
            wakeup.request_wakeup(self.conn, repo_id='repo', reason='manual_operator_request',
                                  dedupe_key=kind, metadata={'kind': kind})
        self.conn.execute("UPDATE wakeups SET metadata_json='bad-json' WHERE dedupe_key='operator_decision'")
        wakeup.request_wakeup(self.conn, repo_id='repo', reason='worker_result_ready',
                              dedupe_key='worker', metadata={'kind': 'github_observation_ready'})
        summary = wakeup.summarize_wakeups(self.conn)
        self.assertEqual(summary['by_reason'], {'github_observation_ready': 1, 'github_context_refresh': 1,
                                               'manual_operator_request': 1, 'worker_result_ready': 1})
        self.assertEqual(summary['total'], 4)
        self.assertEqual(summary['pending'], 4)
        self.assertEqual(summary['due'], 4)

    def test_logical_reason_reaches_operations_response_and_status_cli(self):
        from robert_agent import status, web
        wakeup.request_wakeup(self.conn, repo_id='repo', reason='manual_operator_request',
                              dedupe_key='scan', metadata={'kind': 'github_observation_ready'})
        self.conn.commit()
        data = web.build_dashboard_data(self.db)
        self.assertEqual(data['wakeup_summary']['by_reason'], {'github_observation_ready': 1})
        self.assertEqual(data['recent_wakeups'][0]['reason'], 'github_observation_ready')
        serialized = status.build_status(self.db)
        self.assertEqual(serialized['wakeup_summary']['by_reason'], {'github_observation_ready': 1})

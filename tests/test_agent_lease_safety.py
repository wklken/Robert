from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from robert_agent import run_once, storage


class AgentLeaseSafetyTests(unittest.TestCase):
    def test_expired_lease_requires_owner_exit_evidence(self):
        for state in ('alive_same', 'unknown', 'dead'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / 'robert.sqlite3'
                storage.init_database(db)
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute("INSERT INTO agent_runs(run_id,status,started_at,summary_json) VALUES ('old','running','2000-01-01',?)",
                                 (json.dumps({'executor_identity': {'pid': 123}}),))
                    run_once._acquire_agent_lease(conn, 'repo', 'old', '2000-01-01T00:00:00+00:00', 1)
                    with mock.patch.object(run_once.worker_identity, 'inspect_recorded_identity', return_value=state):
                        result = run_once._acquire_agent_lease(conn, 'repo', 'new', '2026-10-29T00:00:00+00:00', 1)
                    self.assertEqual(result['ok'], state == 'dead')
                    owners = conn.execute("SELECT owner_id FROM leases WHERE status='active'").fetchall()
                    self.assertEqual(owners, [('new' if state == 'dead' else 'old',)])

    def test_expired_lease_without_owner_identity_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'robert.sqlite3'
            storage.init_database(db)
            with closing(sqlite3.connect(db)) as conn, conn:
                run_once._acquire_agent_lease(conn, 'repo', 'old', '2000-01-01T00:00:00+00:00', 1)
                result = run_once._acquire_agent_lease(conn, 'repo', 'new', '2026-10-29T00:00:00+00:00', 1)
                self.assertFalse(result['ok'])

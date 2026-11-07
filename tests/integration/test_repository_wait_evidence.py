"""Acceptance gaps left by the first reliability delivery."""


from contextlib import closing

import sqlite3

import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.runtime import cycle as run_once

from robert_agent.execution import leases as execution_leases


from robert_agent.foundation import values


class WaitingEvidenceTests(unittest.TestCase):
    def test_repository_lease_wait_is_visible_preserved_then_cleared(self):
        from tests.support.repository_cycle import RepositoryCycleFixture
        from robert_agent.readmodels import execution_metrics
        fixture=RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        first=run_once.run_once(fixture.config_path,fixture_path=fixture.fixture_path,dry_run=True,skip_external=True)
        self.assertTrue(first['ok'],first)
        db=fixture.data_dir/'dd.sqlite3'
        now=values.utc_now()
        with closing(sqlite3.connect(db)) as conn,conn:
            task=conn.execute('SELECT task_id FROM tasks').fetchone()[0]
            lease=execution_leases.acquire(conn,'repo:example/backend','other-run',now,9)
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
            execution_leases.release(conn,lease['lease_id'],values.utc_now())
        run_once.run_once(fixture.config_path,dry_run=True,skip_external=True,skip_publish=True)
        with closing(sqlite3.connect(db)) as conn:
            self.assertNotEqual(execution_metrics.task_timing(conn,task)['waiting_reason'],'repository_busy')

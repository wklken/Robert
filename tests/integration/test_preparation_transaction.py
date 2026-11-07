"""Regression contracts for bounded recovery and durable user outcomes."""

from contextlib import closing


import sqlite3


import unittest

from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.execution import workspace as assigned_workspace


class TransactionBoundaryTests(unittest.TestCase):
    def test_worktree_preparation_does_not_hold_domain_write_lock(self):
        from robert_agent.runtime import cycle as run_once
        from tests.support.repository_cycle import RepositoryCycleFixture

        fixture = RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = assigned_workspace.prepare_worktree
        checked = []

        def prepare(*args, **kwargs):
            with closing(sqlite3.connect(fixture.data_dir / "dd.sqlite3", timeout=0.01)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            checked.append(True)
            return original(*args, **kwargs)

        with mock.patch.object(assigned_workspace, "prepare_worktree", side_effect=prepare):
            result = run_once.run_once(
                fixture.config_path,
                fixture_path=fixture.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(checked)

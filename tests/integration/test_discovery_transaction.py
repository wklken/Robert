"""Acceptance gaps left by the first reliability delivery."""

from contextlib import closing

import sqlite3

import unittest

from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401


from robert_agent.runtime import cycle as run_once


from robert_agent.adapters.github import discovery


class DiscoveryLockTests(unittest.TestCase):
    def test_external_discovery_does_not_hold_domain_writer_lock(self):
        from tests.support.repository_cycle import RepositoryCycleFixture

        fixture = RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        calls = []

        def discover(*args, **kwargs):
            with closing(sqlite3.connect(fixture.data_dir / "dd.sqlite3", timeout=0.1)) as other:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            calls.append("unlocked")
            return []

        with (
            patch.object(discovery, "collect_account_notifications", return_value={}),
            patch.object(discovery, "collect_live_events", side_effect=discover),
        ):
            response = run_once.run_once(fixture.config_path, dry_run=True, skip_external=False)
        self.assertTrue(response["ok"], response)
        self.assertEqual(calls, ["unlocked"])

"""Regression contracts for bounded recovery and durable user outcomes."""


from pathlib import Path


import tempfile


import unittest

from unittest import mock

from tests.support import PACKAGE_ROOT  # noqa: F401

from robert_agent.adapters.sqlite import database as storage


class SpoolValidationTests(unittest.TestCase):
    def test_malformed_shape_is_quarantined_without_blocking_replay(self):
        from robert_agent.execution import spool as result_spool

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db"
            storage.init_database(db)
            root = result_spool.directory(db)
            root.mkdir()
            (root / "bad.json").write_text("[]")
            result = result_spool.replay(db, mock.Mock())
            self.assertEqual(result["rejected"], 1)
            self.assertTrue((root / "bad.invalid").exists())

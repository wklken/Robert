"""Retain process-group cleanup proof when dispatch loses its claim."""

import unittest
from unittest import mock

from robert_agent.adapters.processes import worker


class DispatchCleanupTests(unittest.TestCase):
    def test_proven_startup_cleanup_needs_no_identity_reinspection(self):
        with mock.patch.object(worker.worker_identity, "terminate_recorded_identity") as terminate:
            self.assertTrue(worker.stop_dispatch({"pid": 12345678, "cleanup_pending": False, "process_exited": True}))
        terminate.assert_not_called()

    def test_fast_exited_launch_requires_group_exit(self):
        for live in (False, True):
            with (
                self.subTest(live=live),
                mock.patch.object(worker.worker_identity, "live_process_group", return_value=live),
                mock.patch.object(worker.worker_identity, "terminate_recorded_identity") as terminate,
            ):
                self.assertEqual(worker.stop_dispatch({"pid": 12345678, "process_exited": True}), not live)
                terminate.assert_not_called()

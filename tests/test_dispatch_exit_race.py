import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from robert_agent import dispatch, worker_identity


class DispatchExitRaceTests(unittest.TestCase):
    def _dispatch(self, returncode, *, identity=None, error=None, cleaned_up=True):
        proc = Mock(pid=12345)
        # The startup probe sees a live child; it exits during identity capture.
        proc.poll.side_effect = [None, returncode]
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("task prompt", encoding="utf-8")
            with (
                patch.object(dispatch.subprocess, "Popen", return_value=proc),
                patch.object(
                    worker_identity, "capture_process_identity",
                    return_value=identity, side_effect=error,
                ) as capture,
                patch.object(
                    worker_identity, "cleanup_new_process", return_value=cleaned_up,
                ) as cleanup,
                patch.dict(dispatch._BACKGROUND_PROCESSES, {}, clear=True),
            ):
                result = dispatch.dispatch_worker(
                    "task-1", "attempt-1", prompt,
                    worker_agent="command", worker_command=[sys.executable],
                    dry_run=False, startup_probe_seconds=0,
                )
                capture.assert_called_once_with(proc.pid)
                self.assertNotIn(proc.pid, dispatch._BACKGROUND_PROCESSES)
        return result, proc, cleanup

    def _assert_exited(self, result, proc, cleanup, returncode):
        self.assertEqual(result["ok"], returncode == 0, result)
        self.assertEqual(result["status"], "running" if returncode == 0 else "failed_dispatch")
        self.assertTrue(result["process_exited"])
        self.assertEqual(result["returncode"], returncode)
        self.assertEqual(result["pid"], proc.pid)
        self.assertNotIn("identity_capture_failed", result)
        self.assertNotIn("process_start_token", result)
        self.assertEqual(proc.poll.call_count, 2)
        if returncode == 0:
            cleanup.assert_not_called()
        else:
            cleanup.assert_called_once_with(proc)
            self.assertEqual(result["safe_error"], f"worker exited immediately with code {returncode}")

    def test_exit_during_identity_capture_uses_child_returncode(self):
        for returncode in (0, 7):
            for error_type in (worker_identity.ProcessIdentityMissing, worker_identity.IdentityReadError):
                with self.subTest(returncode=returncode, error_type=error_type):
                    result, proc, cleanup = self._dispatch(
                        returncode, error=error_type("identity unavailable"),
                    )
                    self._assert_exited(result, proc, cleanup, returncode)

    def test_exit_during_incomplete_or_dead_identity_uses_child_returncode(self):
        complete = {
            "pid": 12345,
            "process_group_id": 12345,
            "process_start_token": "start",
            "command_fingerprint": "a" * 64,
            "identity_source": "ps",
            "process_state": "R",
        }
        for identity in (None, {}, {**complete, "process_state": "Z"},
                         {**complete, "command_fingerprint": "invalid"}):
            for returncode in (0, 7):
                with self.subTest(identity=identity, returncode=returncode):
                    result, proc, cleanup = self._dispatch(returncode, identity=identity)
                    self._assert_exited(result, proc, cleanup, returncode)

    def test_live_child_identity_capture_failure_still_fails_and_cleans_up(self):
        for cleaned_up in (True, False):
            with self.subTest(cleaned_up=cleaned_up):
                result, proc, cleanup = self._dispatch(
                    None, error=worker_identity.IdentityReadError("permission denied"),
                    cleaned_up=cleaned_up,
                )
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["status"], "failed_dispatch")
                self.assertEqual(result["safe_error"], "worker process identity could not be captured")
                self.assertEqual(result["error_type"], "IdentityReadError")
                self.assertTrue(result["identity_capture_failed"])
                self.assertEqual(result["cleanup_pending"], not cleaned_up)
                self.assertNotIn("process_exited", result)
                cleanup.assert_called_once_with(proc)

    def test_live_child_incomplete_identity_still_fails_and_cleans_up(self):
        for identity in (None, {}, {"process_state": "Z"}):
            for cleaned_up in (True, False):
                with self.subTest(identity=identity, cleaned_up=cleaned_up):
                    result, proc, cleanup = self._dispatch(
                        None, identity=identity, cleaned_up=cleaned_up,
                    )
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["status"], "failed_dispatch")
                    self.assertEqual(result["safe_error"], "worker process identity was incomplete")
                    self.assertTrue(result["identity_capture_failed"])
                    self.assertEqual(result["cleanup_pending"], not cleaned_up)
                    self.assertNotIn("process_exited", result)
                    cleanup.assert_called_once_with(proc)

    def test_nonzero_exit_during_capture_preserves_pending_cleanup(self):
        result, proc, cleanup = self._dispatch(
            7, error=worker_identity.ProcessIdentityMissing("exited"), cleaned_up=False,
        )
        self._assert_exited(result, proc, cleanup, 7)
        self.assertTrue(result["cleanup_pending"])

from contextlib import closing
from pathlib import Path
import sqlite3
from unittest import mock
from tests.support.daemon import DaemonFixture


class DaemonGracefulStopTests(DaemonFixture):
    def _request_stop_later(self, delay=0.3):
        import threading
        from robert_agent.runtime import daemon

        timer = threading.Timer(delay, daemon._request_stop, (daemon.signal.SIGTERM, None))
        timer.start()
        self.addCleanup(timer.cancel)

    def test_stop_request_terminates_running_child_group_promptly(self):
        import os
        import sys
        import time
        from robert_agent.runtime import daemon

        pid_file = self.root / "grandchild.pid"
        script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        self._request_stop_later()
        started = time.monotonic()

        result = daemon.run_child_json([sys.executable, "-c", script], timeout_seconds=60)

        self.assertLess(time.monotonic() - started, 15)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "child_stopped")
        self.assertTrue(result["child"]["process_group_stopped"], result)
        grandchild = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild, 0)

    def test_stop_request_prevents_starting_another_child(self):
        from robert_agent.runtime import daemon

        daemon._STOP_REQUESTED = True
        with mock.patch.object(daemon.subprocess, "Popen") as popen:
            result = daemon.run_child_json(["python3", "never.py"], timeout_seconds=60)

        popen.assert_not_called()
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "child_stopped")

    def test_stop_signal_wakes_the_idle_listener(self):
        import time
        from robert_agent.runtime import daemon
        from robert_agent.adapters.notifications import wakeup as wakeup_notifier

        handlers = {}
        with mock.patch.object(daemon.signal, "signal", side_effect=lambda sig, fn: handlers.setdefault(sig, fn)):
            daemon._install_stop_handlers(self.db_path)
        listener = wakeup_notifier.WakeupListener(self.db_path)
        self.addCleanup(listener.close)

        handlers[daemon.signal.SIGTERM](daemon.signal.SIGTERM, None)
        started = time.monotonic()
        woke = listener.wait(30)

        self.assertTrue(woke)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(daemon._STOP_REQUESTED)

    def test_default_sleep_returns_when_stop_is_requested(self):
        import time
        from robert_agent.runtime import daemon

        self._request_stop_later()
        started = time.monotonic()
        daemon._stop_aware_sleep(30)

        self.assertLess(time.monotonic() - started, 3)

    def test_service_daemon_waits_for_a_stale_lease_instead_of_exiting(self):
        from robert_agent.runtime import daemon

        config_path = self._hold_lease("daemon-run-killed", ttl_seconds=1)
        with (
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(daemon, "_resident_loop",
                              side_effect=lambda context: {"ok": True, "status": "stopped",
                                                           "daemon_run_id": context.daemon_run_id}),
        ):
            result = daemon.run_daemon(config_path, wait_for_lease=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "stopped")

    def test_lease_wait_ends_cleanly_when_stop_is_requested(self):
        from robert_agent.runtime import daemon

        config_path = self._hold_lease("daemon-run-live", ttl_seconds=600)
        self._request_stop_later()
        with (
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(daemon, "_resident_loop") as resident,
        ):
            result = daemon.run_daemon(config_path, wait_for_lease=True)

        resident.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "stopped_waiting_for_lease")

    def test_manual_daemon_still_reports_an_active_lease(self):
        from robert_agent.runtime import daemon

        config_path = self._hold_lease("daemon-run-live", ttl_seconds=600)
        with mock.patch.object(daemon, "_resident_loop") as resident:
            result = daemon.run_daemon(config_path)

        resident.assert_not_called()
        self.assertEqual(result["status"], "skipped_active_daemon")

    def test_daemon_main_accepts_wait_for_lease(self):
        from robert_agent.runtime import daemon

        with mock.patch.object(daemon, "run_daemon", return_value={"ok": True}) as run:
            daemon.main(["--config", "c.yml", "--wait-for-lease"])

        self.assertTrue(run.call_args.kwargs["wait_for_lease"])

    def _hold_lease(self, owner_id, ttl_seconds):
        from robert_agent.execution import runtime_state as daemon_state
        from robert_agent.configuration import command as validate_config

        config_path = self._write_config()
        self._init_db()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
        config = validate_config.validate_config(config_path, skip_external=True)
        with closing(daemon_state.connect(self.db_path)) as conn, conn:
            lease = daemon_state.acquire_daemon_lease(
                conn, resource_key=f"database:{Path(config['db_path']).resolve()}",
                owner_id=owner_id, ttl_seconds=ttl_seconds)
        self.assertTrue(lease["ok"], lease)
        return config_path

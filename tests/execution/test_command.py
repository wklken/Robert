from contextlib import closing
import json
import sys
import sqlite3
import unittest
from pathlib import Path
from robert_agent.execution.application import evidence as cycle_evidence
from tests.support.worker_protocol import WorkerProtocolFixture


class CommandJournalTests(WorkerProtocolFixture):
    def test_heartbeat_command_record_tracks_active_command(self):
        from robert_agent.execution import command as heartbeat

        record = heartbeat.build_command_record(
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            command=["python", "-m", "unittest"],
        )

        self.assertEqual(record["phase"], "verify")
        self.assertEqual(record["active_command"], ["python", "-m", "unittest"])

    def test_heartbeat_executes_command_and_records_phase(self):
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()

        command_result = heartbeat.run_command_with_heartbeat(
            db_path=db_path,
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            command=[
                sys.executable,
                "-c",
                "import time; time.sleep(0.35); print('ok')",
            ],
            timeout_seconds=5,
            heartbeat_interval_seconds=0.1,
        )

        self.assertTrue(command_result["ok"], command_result)
        self.assertEqual(command_result["returncode"], 0)
        self.assertNotIn("stdout", command_result)
        self.assertNotIn("stderr", command_result)
        self.assertIn("ok", command_result["stdout_tail"])
        self.assertGreater(command_result["stdout_bytes"], 0)
        self.assertTrue(Path(command_result["stdout_path"]).exists())
        self.assertTrue(Path(command_result["stderr_path"]).exists())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            phase_count = conn.execute("SELECT COUNT(*) FROM worker_phases").fetchone()[0]
            summaries = [row[0] for row in conn.execute("SELECT summary FROM worker_phases")]
        self.assertGreaterEqual(phase_count, 3)
        self.assertTrue(any("still running" in summary for summary in summaries))

    def test_heartbeat_returns_only_tail_for_large_output(self):
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()

        command_result = heartbeat.run_command_with_heartbeat(
            db_path=db_path,
            task_id="task-1",
            attempt_id="attempt-1",
            phase="verify",
            command=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('A' * 200 + 'TAIL')",
            ],
            timeout_seconds=5,
            heartbeat_interval_seconds=0.1,
            tail_bytes=16,
            tail_lines=80,
        )

        self.assertTrue(command_result["ok"], command_result)
        self.assertNotIn("stdout", command_result)
        self.assertEqual(command_result["stdout_tail"], "AAAAAAAAAAAATAIL")
        self.assertTrue(command_result["stdout_truncated"])
        self.assertGreater(command_result["stdout_bytes"], len(command_result["stdout_tail"]))
        self.assertEqual(
            Path(command_result["stdout_path"]).read_text(encoding="utf-8"),
            "A" * 200 + "TAIL",
        )

    def test_heartbeat_preserves_command_result_when_snapshots_are_locked(self):
        from unittest.mock import patch
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        for locked_summary in (
            "Starting command",
            "Command still running",
            "Command exited 0",
        ):
            with self.subTest(locked_summary=locked_summary):
                original = heartbeat.snapshot.record_snapshot

                def record_snapshot(**kwargs):
                    if kwargs["summary"] == locked_summary:
                        raise sqlite3.OperationalError("database is locked")
                    return original(**kwargs)

                with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=record_snapshot):
                    try:
                        result = heartbeat.run_command_with_heartbeat(
                            db_path,
                            "task-1",
                            "attempt-1",
                            "verify",
                            [
                                sys.executable,
                                "-c",
                                "import time; time.sleep(.25); print('delivered')",
                            ],
                            timeout_seconds=5,
                            heartbeat_interval_seconds=0.1,
                        )
                    except sqlite3.OperationalError as exc:
                        self.fail(f"SQLite contention must not lose command completion: {exc}")
                self.assertEqual(result["returncode"], 0)
                self.assertEqual(result["stdout_tail"].strip(), "delivered")
                record = json.loads(Path(result["command_record_path"]).read_text())
                self.assertEqual(record["status"], "completed")
                self.assertEqual(record["returncode"], 0)
                self.assertEqual(record["attempt_id"], "attempt-1")
                if locked_summary == "Command exited 0":
                    self.assertEqual(result["snapshot_status"], "deferred")

    def test_heartbeat_journals_completion_before_database_write(self):
        from unittest.mock import patch
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        original = heartbeat.snapshot.record_snapshot
        observed = []

        def record_snapshot(**kwargs):
            if kwargs["summary"] == "Command exited 7":
                records = list((self.root / "artifacts" / "task-1").glob("*.command.json"))
                self.assertEqual(len(records), 1, "Persist exit evidence before the SQLite write")
                record = json.loads(records[0].read_text())
                self.assertEqual(record["returncode"], 7)
                self.assertEqual(record["status"], "failed")
                observed.append(record)
                raise sqlite3.OperationalError("database is locked")
            return original(**kwargs)

        with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=record_snapshot):
            result = heartbeat.run_command_with_heartbeat(
                db_path,
                "task-1",
                "attempt-1",
                "verify",
                [
                    sys.executable,
                    "-c",
                    "import sys; print('failed evidence'); sys.exit(7)",
                ],
                heartbeat_interval_seconds=0.1,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["snapshot_status"], "deferred")
        self.assertEqual(len(observed), 1)

    def test_heartbeat_timeout_has_durable_terminal_record(self):
        from robert_agent.execution import command as heartbeat

        result = heartbeat.run_command_with_heartbeat(
            self._init_attempt_db(),
            "task-1",
            "attempt-1",
            "verify",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout_seconds=0.15,
            heartbeat_interval_seconds=0.1,
        )
        self.assertFalse(result["ok"])
        self.assertIsNone(result["returncode"])
        record = json.loads(Path(result["command_record_path"]).read_text())
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["status"], "failed")

    def test_heartbeat_completes_under_real_sqlite_writer_lock(self):
        import threading
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path, check_same_thread=False)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            release = threading.Timer(0.2, writer.rollback)
            release.start()
            self.addCleanup(release.join)
            result = heartbeat.run_command_with_heartbeat(
                db_path,
                "task-1",
                "attempt-1",
                "verify",
                [sys.executable, "-c", "print('real contention')"],
                heartbeat_interval_seconds=0.1,
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["snapshot_status"], "recorded")
        self.assertIn("real contention", result["stdout_tail"])

    def test_heartbeat_invalid_attempt_does_not_launch_command_when_sqlite_locked(self):
        from unittest.mock import patch
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        marker = self.root / "unauthorized-command"
        with patch.object(
            heartbeat.snapshot,
            "record_snapshot",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.assertRaises(ValueError):
                heartbeat.run_command_with_heartbeat(
                    db_path,
                    "wrong-task",
                    "attempt-1",
                    "verify",
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).touch()",
                    ],
                    heartbeat_interval_seconds=0.1,
                )
        self.assertFalse(marker.exists())

    def test_heartbeat_unexpected_snapshot_error_reaps_child_and_records_failure(self):
        from unittest.mock import patch
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        original = heartbeat.snapshot.record_snapshot

        def record_snapshot(**kwargs):
            if kwargs["summary"] == "Command still running":
                raise sqlite3.OperationalError("no such table: worker_phases")
            return original(**kwargs)

        with patch.object(heartbeat.snapshot, "record_snapshot", side_effect=record_snapshot):
            with self.assertRaises(sqlite3.OperationalError):
                heartbeat.run_command_with_heartbeat(
                    db_path,
                    "task-1",
                    "attempt-1",
                    "verify",
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    heartbeat_interval_seconds=0.1,
                )
        record = json.loads(next((self.root / "artifacts" / "task-1").glob("*.command.json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertIsNotNone(record["returncode"])

    @unittest.skipUnless(sys.platform != "win32", "POSIX command process groups")
    def test_heartbeat_timeout_reaps_command_descendants(self):
        import os
        import signal
        from robert_agent.execution import command as heartbeat

        pid_path = self.root / "descendant.pid"
        child_script = "import time; time.sleep(30)"
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c',{child_script!r}]); "
            f"Path({str(pid_path)!r}).write_text(str(p.pid)); time.sleep(30)"
        )
        result = heartbeat.run_command_with_heartbeat(
            self._init_attempt_db(),
            "task-1",
            "attempt-1",
            "verify",
            [sys.executable, "-c", script],
            timeout_seconds=0.3,
            heartbeat_interval_seconds=0.1,
        )
        pid = int(pid_path.read_text())
        try:
            self.assertEqual(cycle_evidence._command_process_status(pid, ""), "not_found")
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertFalse(result["ok"])

    def test_heartbeat_keeps_commands_in_worker_process_group(self):
        import os
        from robert_agent.execution import command as heartbeat

        result = heartbeat.run_command_with_heartbeat(
            self._init_attempt_db(),
            "task-1",
            "attempt-1",
            "verify",
            [sys.executable, "-c", "import os; print(os.getpgrp())"],
            heartbeat_interval_seconds=0.1,
        )
        self.assertEqual(
            int(result["stdout_tail"]),
            os.getpgrp(),
            "Worker cancellation must still clean up the whole command group",
        )

    def test_custom_command_output_keeps_journal_discoverable_by_supervisor(self):
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        result = heartbeat.run_command_with_heartbeat(
            db_path,
            "task-1",
            "attempt-1",
            "verify",
            [sys.executable, "-c", "print('custom')"],
            output_dir=self.root / "external-output",
            heartbeat_interval_seconds=0.1,
        )
        self.assertEqual(Path(result["stdout_path"]).parent, self.root / "external-output")
        self.assertEqual(
            Path(result["command_record_path"]).parent,
            self.root / "artifacts" / "task-1",
        )
        with closing(sqlite3.connect(db_path)) as conn:
            recovery = cycle_evidence.inspect_attempt_recovery(conn, "attempt-1", "task-1", None)
        self.assertEqual(recovery["commands"][0]["returncode"], 0)

    def test_command_cleanup_reaps_parent_when_process_inspection_fails(self):
        import subprocess
        from unittest.mock import patch
        from robert_agent.execution import command as heartbeat

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        with patch.object(heartbeat.subprocess, "run", side_effect=OSError("ps unavailable")):
            try:
                heartbeat._kill_command(proc)
            except OSError:
                pass
            self.assertIsNotNone(proc.poll(), "Inspection failure must not skip reaping the command")

    def test_command_start_failure_has_terminal_journal(self):
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        with self.assertRaises(FileNotFoundError):
            heartbeat.run_command_with_heartbeat(
                db_path,
                "task-1",
                "attempt-1",
                "verify",
                [str(self.root / "missing-executable")],
            )
        record = json.loads(next((self.root / "artifacts" / "task-1").glob("*.command.json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["summary"], "Command failed to start: FileNotFoundError")

    def test_replay_after_completion_snapshot_does_not_duplicate_phase(self):
        from robert_agent.execution import command as heartbeat

        db_path = self._init_attempt_db()
        result = heartbeat.run_command_with_heartbeat(
            db_path,
            "task-1",
            "attempt-1",
            "verify",
            [sys.executable, "-c", "print('done')"],
            heartbeat_interval_seconds=0.1,
        )
        path = Path(result["command_record_path"])
        record = json.loads(path.read_text())
        # Simulate wrapper death after SQLite commit but before marking the journal recorded.
        record["snapshot_status"] = "deferred"
        path.write_text(json.dumps(record))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            cycle_evidence.inspect_attempt_recovery(conn, "attempt-1", "task-1", None)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM worker_phases WHERE summary='Command exited 0'",
                ).fetchone()[0],
                1,
            )

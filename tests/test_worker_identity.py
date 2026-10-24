from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class WorkerIdentityTests(unittest.TestCase):
    def test_proc_stat_parser_handles_parentheses_and_extracts_identity_fields(self):
        from robert_agent import worker_identity

        fields = ["S", "1", "4321"] + ["0"] * 16 + ["987654"]
        stat = "77 (worker name (v1)) " + " ".join(fields)
        identity = worker_identity.parse_proc_stat(stat, pid=77)

        self.assertEqual(identity["pid"], 77)
        self.assertEqual(identity["process_state"], "S")
        self.assertEqual(identity["process_group_id"], 4321)
        self.assertEqual(identity["process_start_token"], "987654")

    def test_proc_identity_hashes_observed_cmdline_bytes(self):
        from robert_agent import worker_identity

        stat = "77 (worker) " + " ".join(["R", "1", "77"] + ["0"] * 16 + ["123"])
        cmdline = b"worker\x00--attempt\x00a1\x00"
        identity = worker_identity.read_proc_identity(
            77,
            read_text=lambda _path: stat,
            read_bytes=lambda _path: cmdline,
        )

        self.assertEqual(identity["identity_source"], "proc")
        self.assertEqual(
            identity["command_fingerprint"], hashlib.sha256(cmdline).hexdigest()
        )

    def test_empty_proc_cmdline_is_not_a_running_identity(self):
        from robert_agent import worker_identity

        stat = "77 (worker) " + " ".join(["S", "1", "77"] + ["0"] * 16 + ["123"])
        with tempfile.TemporaryDirectory() as tmp:
            proc_dir = Path(tmp) / "77"
            proc_dir.mkdir()
            (proc_dir / "stat").write_text(stat, encoding="utf-8")
            (proc_dir / "cmdline").write_bytes(b"")
            with self.assertRaises(worker_identity.IdentityReadError):
                worker_identity.capture_process_identity(
                    77,
                    proc_root=tmp,
                    ps_runner=lambda *_args, **_kwargs: self.fail(
                        "empty proc cmdline must not fall back to ps"
                    ),
                )

    def test_captured_zombie_is_not_a_running_identity(self):
        from robert_agent import worker_identity

        stat = "77 (worker) " + " ".join(["Z", "1", "77"] + ["0"] * 16 + ["123"])
        with tempfile.TemporaryDirectory() as tmp:
            proc_dir = Path(tmp) / "77"
            proc_dir.mkdir()
            (proc_dir / "stat").write_text(stat, encoding="utf-8")
            (proc_dir / "cmdline").write_bytes(b"worker\x00")
            with self.assertRaises(worker_identity.IdentityReadError):
                worker_identity.capture_process_identity(77, proc_root=tmp)

    def test_exited_proc_with_empty_cmdline_is_dead(self):
        from unittest.mock import patch
        from robert_agent import worker_identity

        recorded = {
            "pid": 77,
            "process_group_id": 77,
            "process_start_token": "123",
            "command_fingerprint": "f" * 64,
            "identity_source": "proc",
        }
        for states, expected in ((["Z"], "dead"), (["S", "Z"], "dead"), (["S", "S"], "unknown")):
            with self.subTest(states=states):
                stats = ["77 (worker) " + " ".join([state, "1", "77"] + ["0"] * 16 + ["123"]) for state in states]
                with patch.object(worker_identity.os, "kill"), \
                     patch.object(Path, "read_text", side_effect=stats), \
                     patch.object(Path, "read_bytes", return_value=b""):
                    self.assertEqual(worker_identity.inspect_recorded_identity(recorded), expected)

    def test_capturing_exited_proc_does_not_fall_back_to_ps(self):
        from unittest.mock import patch
        from robert_agent import worker_identity

        stat = "77 (worker) " + " ".join(["Z", "1", "77"] + ["0"] * 16 + ["123"])
        with patch.object(Path, "read_text", return_value=stat), \
             patch.object(Path, "read_bytes", return_value=b""):
            with self.assertRaises(worker_identity.ZombieProcess):
                worker_identity.capture_process_identity(
                    77, ps_runner=lambda *_args, **_kwargs: self.fail("must not capture a zombie")
                )

    def test_proc_disappearance_rechecks_pid_before_calling_it_dead(self):
        from robert_agent import worker_identity

        recorded = {
            "pid": 77,
            "process_group_id": 77,
            "process_start_token": "start",
            "command_fingerprint": "f" * 64,
            "identity_source": "proc",
        }
        original_kill = worker_identity.os.kill
        calls = []

        def fake_kill(pid, sig):
            calls.append((pid, sig))
            if len(calls) == 2:
                raise ProcessLookupError()

        try:
            worker_identity.os.kill = fake_kill
            status = worker_identity.inspect_recorded_identity(
                recorded,
                proc_root="/definitely/missing/proc",
            )
        finally:
            worker_identity.os.kill = original_kill

        self.assertEqual(status, "dead")
        self.assertEqual(calls, [(77, 0), (77, 0)])

    def test_proc_disappearance_with_visible_pid_is_unknown(self):
        from robert_agent import worker_identity

        recorded = {
            "pid": 78,
            "process_group_id": 78,
            "process_start_token": "start",
            "command_fingerprint": "f" * 64,
            "identity_source": "proc",
        }
        original_kill = worker_identity.os.kill
        calls = []

        def fake_kill(pid, sig):
            calls.append((pid, sig))

        try:
            worker_identity.os.kill = fake_kill
            status = worker_identity.inspect_recorded_identity(
                recorded,
                proc_root="/definitely/missing/proc",
            )
        finally:
            worker_identity.os.kill = original_kill

        self.assertEqual(status, "unknown")
        self.assertEqual(calls, [(78, 0), (78, 0)])

    def test_identity_mismatch_and_missing_fields_are_unknown(self):
        from robert_agent import worker_identity

        recorded = {
            "pid": 7,
            "process_group_id": 7,
            "process_start_token": "old",
            "command_fingerprint": "f" * 64,
            "identity_source": "proc",
        }
        observed = {**recorded, "process_start_token": "new"}
        self.assertEqual(
            worker_identity.compare_recorded_identity(recorded, observed), "unknown"
        )
        self.assertEqual(
            worker_identity.compare_recorded_identity(
                recorded, {**recorded, "process_state": "Z"}
            ),
            "dead",
        )
        self.assertEqual(
            worker_identity.compare_recorded_identity(
                {**recorded, "process_group_id": None}, observed
            ),
            "unknown",
        )

    def test_terminate_requires_isolated_verified_group_and_rechecks_identity(self):
        from robert_agent import worker_identity

        recorded = {
            "pid": 7,
            "process_group_id": 7,
            "process_start_token": "start",
            "command_fingerprint": "f" * 64,
            "identity_source": "proc",
        }
        states = iter(["alive_same", "alive_same", "dead"])
        signals = []
        result = worker_identity.terminate_recorded_identity(
            recorded,
            inspect=lambda _identity: next(states),
            signal_group=lambda pgid, sig: signals.append((pgid, sig)),
            supervisor_process_group_id=99,
            clock=iter([0.0, 0.0, 0.1, 0.2]).__next__,
            sleep=lambda _seconds: None,
            timeout_seconds=0.15,
        )

        self.assertEqual(result["status"], "terminated")
        self.assertEqual(signals[0][0], 7)
        self.assertEqual(signals[0][1], getattr(__import__("signal"), "SIGTERM"))

    def test_termination_waits_for_confirmed_exit_after_identity_loss(self):
        from robert_agent import worker_identity

        recorded = {
            "pid": 7, "process_group_id": 7, "process_start_token": "123",
            "command_fingerprint": "f" * 64, "identity_source": "proc",
        }
        states = iter(["alive_same", "unknown", "dead"])
        signals = []
        result = worker_identity.terminate_recorded_identity(
            recorded, inspect=lambda _: next(states),
            signal_group=lambda *args: signals.append(args),
            supervisor_process_group_id=99, sleep=lambda _: None,
        )
        self.assertEqual(result["status"], "terminated")
        self.assertEqual(len(signals), 1)

    def test_identity_loss_never_escalates_even_if_identity_returns(self):
        from robert_agent import worker_identity

        recorded = {
            "pid": 7, "process_group_id": 7, "process_start_token": "123",
            "command_fingerprint": "f" * 64, "identity_source": "proc",
        }
        states = iter(["alive_same", "unknown"])
        elapsed = [0.0]
        signals = []

        def sleep(seconds):
            elapsed[0] += seconds

        result = worker_identity.terminate_recorded_identity(
            recorded, inspect=lambda _: next(states, "alive_same"),
            signal_group=lambda *args: signals.append(args),
            supervisor_process_group_id=99, clock=lambda: elapsed[0],
            sleep=sleep, timeout_seconds=.1,
        )
        self.assertEqual(result["status"], "attention")
        self.assertEqual(len(signals), 1)
        self.assertGreaterEqual(elapsed[0], .1)

    def test_db_backed_identity_operations_load_only_attempt_dispatch_metadata(self):
        from robert_agent import storage, workflow_operations

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "robert.sqlite3"
            storage.init_database(db_path)
            now = "2026-08-02T00:00:00+00:00"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, ?, ?, ?, ?)",
                    ("repo:test", "example/test", "bot", "main", tmp, tmp),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
                    ("ws:test", "repo:test", now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, task_kind, lifecycle, created_at, updated_at) VALUES (?, ?, 'human_request', 'running', ?, ?)",
                    ("task:test", "ws:test", now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, metadata_json) VALUES (?, ?, 1, 'running', ?)",
                    (
                        "attempt:test",
                        "task:test",
                        json.dumps(
                            {
                                "dispatch": {
                                    "pid": 7,
                                    "process_group_id": 7,
                                    "process_start_token": "start",
                                    "command_fingerprint": "f" * 64,
                                    "identity_source": "proc",
                                }
                            }
                        ),
                    ),
                )

            original = workflow_operations.worker_identity.inspect_recorded_identity
            workflow_operations.worker_identity.inspect_recorded_identity = (
                lambda _identity: "alive_same"
            )
            try:
                self.assertEqual(
                    workflow_operations.inspect_worker_identity(db_path, "attempt:test"),
                    "alive_same",
                )
            finally:
                workflow_operations.worker_identity.inspect_recorded_identity = original

    def test_terminate_worker_persists_redacted_result_once_and_marks_attention(self):
        from robert_agent import storage, workflow_operations, workflow_store

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "robert.sqlite3"
            storage.init_database(db_path)
            now = "2026-08-02T00:00:00+00:00"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, ?, ?, ?, ?)",
                    ("repo:test", "example/test", "bot", "main", tmp, tmp),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
                    ("ws:test", "repo:test", now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, task_kind, lifecycle, created_at, updated_at) VALUES (?, ?, 'human_request', 'running', ?, ?)",
                    ("task:test", "ws:test", now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, metadata_json) VALUES (?, ?, 1, 'running', ?)",
                    (
                        "attempt:test",
                        "task:test",
                        json.dumps(
                            {
                                "dispatch": {
                                    "pid": 7,
                                    "process_group_id": 7,
                                    "process_start_token": "start",
                                    "command_fingerprint": "f" * 64,
                                    "identity_source": "proc",
                                },
                                "existing": "preserved",
                            }
                        ),
                    ),
                )
                workflow_store.bind_subject(
                    conn, "task", "task:test", "human_request", "legacy", now
                )

            original = workflow_operations.worker_identity.terminate_recorded_identity
            workflow_operations.worker_identity.terminate_recorded_identity = (
                lambda _identity: {
                    "status": "attention",
                    "reason": "identity_changed",
                    "pid": 7,
                    "secret": "must-not-persist",
                }
            )
            try:
                self.assertEqual(
                    workflow_operations.terminate_worker(db_path, "attempt:test"),
                    "attention",
                )
                self.assertEqual(
                    workflow_operations.terminate_worker(db_path, "attempt:test"),
                    "attention",
                )
            finally:
                workflow_operations.worker_identity.terminate_recorded_identity = original

            with closing(sqlite3.connect(db_path)) as conn, conn:
                metadata_json = conn.execute(
                    "SELECT metadata_json FROM attempts WHERE attempt_id = ?",
                    ("attempt:test",),
                ).fetchone()[0]
                notification_count = conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE notification_type = 'worker_termination'"
                ).fetchone()[0]
                binding_status = conn.execute(
                    "SELECT binding_status FROM workflow_bindings WHERE subject_type = 'task' AND subject_id = 'task:test'"
                ).fetchone()[0]

            metadata = json.loads(metadata_json)
            self.assertEqual(metadata["existing"], "preserved")
            self.assertEqual(
                metadata["termination"],
                {"status": "attention", "reason": "identity_changed", "pid": 7},
            )
            self.assertEqual(notification_count, 1)
            self.assertEqual(binding_status, "attention")


if __name__ == "__main__":
    unittest.main()

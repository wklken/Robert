from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage, workflow_operations, workflow_signals, workflow_store


NOW = "2026-08-02T00:00:00+00:00"


class WorkflowSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                ) VALUES ('repo-1', 'example/repo', 'bot', 'main', '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                ) VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)
                """,
                (NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle, created_at, updated_at
                ) VALUES ('task-1', 'ws-1', 'human_request', 'running', ?, ?)
                """,
                (NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at
                ) VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (NOW, NOW),
            )

    def _binding(self, owner="dbos"):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            return workflow_store.bind_subject(
                conn, "task", "task-1", "human_request", owner, NOW
            )

    def test_record_subject_signal_builds_id_only_message_and_dedupes(self):
        binding = self._binding()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            first = workflow_signals.record_subject_signal(
                conn,
                binding,
                "worker_result",
                "result-1",
                {"fact_id": "result-1"},
                NOW,
            )
            same = workflow_signals.record_subject_signal(
                conn,
                binding,
                "worker_result",
                "result-1",
                {"fact_id": "result-1"},
                NOW,
            )

        self.assertEqual(first, same)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(
            json.loads(first["payload_json"]),
            {
                "signal_id": first["signal_id"],
                "topic": "worker_result",
                "subject_type": "task",
                "subject_id": "task-1",
                "fact_id": "result-1",
            },
        )

    def test_legacy_binding_does_not_queue_a_signal(self):
        binding = self._binding(owner="legacy")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            with self.assertRaisesRegex(ValueError, "DBOS-owned"):
                workflow_signals.record_subject_signal(
                    conn,
                    binding,
                    "worker_result",
                    "result-1",
                    {"fact_id": "result-1"},
                    NOW,
                )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0],
                0,
            )

    def test_relay_sends_then_marks_delivered(self):
        binding = self._binding()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            signal = workflow_signals.record_subject_signal(
                conn,
                binding,
                "worker_result",
                "result-1",
                {"fact_id": "result-1"},
                NOW,
            )

        calls = []

        def sender(*args, **kwargs):
            calls.append((args, kwargs))

        result = workflow_signals.relay_due_signals(
            self.db_path,
            sender,
            limit=10,
            now=NOW,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(calls[0][0][0], binding["workflow_id"])
        self.assertEqual(calls[0][1]["topic"], "subject-events")
        self.assertEqual(calls[0][1]["idempotency_key"], signal["signal_id"])
        self.assertEqual(calls[0][0][1]["signal_id"], signal["signal_id"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM workflow_outbox WHERE signal_id = ?",
                (signal["signal_id"],),
            ).fetchone()[0]
        self.assertEqual(status, "delivered")

    def test_transient_failure_keeps_pending_and_redacts_error_with_backoff(self):
        binding = self._binding()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            signal = workflow_signals.record_subject_signal(
                conn,
                binding,
                "worker_result",
                "result-1",
                {"fact_id": "result-1"},
                NOW,
            )

        secret = "postgresql://operator:secret@example.invalid/robert"

        def sender(*_args, **_kwargs):
            raise RuntimeError(secret)

        result = workflow_signals.relay_due_signals(
            self.db_path,
            sender,
            limit=10,
            now=NOW,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["failed"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status, attempt_count, not_before_at, last_error_json FROM workflow_outbox WHERE signal_id = ?",
                (signal["signal_id"],),
            ).fetchone()
        self.assertEqual(row[0:2], ("pending", 1))
        self.assertEqual(row[2], "2026-08-02T00:00:02+00:00")
        self.assertEqual(json.loads(row[3]), {"exception_class": "RuntimeError"})
        self.assertNotIn(secret, row[3])

    def test_duplicate_send_before_marking_is_harmless(self):
        binding = self._binding()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            signal = workflow_signals.record_subject_signal(
                conn,
                binding,
                "worker_result",
                "result-1",
                {"fact_id": "result-1"},
                NOW,
            )

        calls = []

        def sender(*_args, **_kwargs):
            calls.append(True)

        sender(binding["workflow_id"], {"signal_id": signal["signal_id"]})
        first = workflow_signals.relay_due_signals(
            self.db_path, sender, limit=10, now=NOW
        )
        second = workflow_signals.relay_due_signals(
            self.db_path, sender, limit=10, now=NOW
        )

        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(calls), 2)

    def test_validation_conflict_does_not_send_and_requires_attention(self):
        binding = self._binding()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            signal = workflow_signals.record_subject_signal(
                conn,
                binding,
                "worker_result",
                "result-1",
                {"fact_id": "result-1"},
                NOW,
            )
            conn.execute(
                "UPDATE workflow_outbox SET payload_json = ? WHERE signal_id = ?",
                (json.dumps({"fact_id": "result-1", "secret": "no"}), signal["signal_id"]),
            )

        calls = []
        result = workflow_signals.relay_due_signals(
            self.db_path,
            lambda *_args, **_kwargs: calls.append(True),
            limit=10,
            now=NOW,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            status, binding_status = conn.execute(
                """
                SELECT o.status, b.binding_status
                FROM workflow_outbox o
                JOIN workflow_bindings b ON b.workflow_id = o.workflow_id
                WHERE o.signal_id = ?
                """,
                (signal["signal_id"],),
            ).fetchone()
        self.assertEqual(status, "attention")
        self.assertEqual(binding_status, "attention")

    def test_recorded_invalidation_signal_can_relay_after_binding_attention(self):
        self._binding()
        fact = {
            "fact_id": "event:head-change",
            "subject_type": "task",
            "subject_id": "task-1",
            "repo": "example/repo",
            "source_type": "pull_request",
            "source_key": "github:example/repo!7",
            "number": 7,
            "event_type": "snapshot",
            "old_head_sha": "a" * 40,
            "new_head_sha": "b" * 40,
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            result = workflow_operations.apply_classified_fact(
                conn, "task", "task-1", fact, now=NOW
            )
        calls = []
        relayed = workflow_signals.relay_due_signals(
            self.db_path,
            lambda *args, **kwargs: calls.append((args, kwargs)),
            limit=10,
            now=NOW,
        )
        self.assertEqual(relayed["delivered"], 1)
        self.assertEqual(calls[0][0][1]["fact_id"], "event:head-change")
        self.assertEqual(result["binding_status"], "attention")


if __name__ == "__main__":
    unittest.main()

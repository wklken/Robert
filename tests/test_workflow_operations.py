from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage, workflow_store


NOW = "2026-08-02T00:00:00+00:00"


class WorkflowOperationsTests(unittest.TestCase):
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
                ) VALUES ('repo-1', 'example/repo', 'robert-bot', 'main',
                          '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  title, state
                ) VALUES ('source-1', 'repo-1', 'github:example/repo#1',
                          'issue', 1, 'Request', 'open')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  active_task_id, created_at, updated_at
                ) VALUES ('workstream-1', 'repo-1', 'source-1', 'active',
                          'task-1', ?, ?)
                """,
                (NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle,
                  created_at, updated_at, metadata_json
                ) VALUES ('task-1', 'workstream-1', 'human_request',
                          'running', ?, ?, ?)
                """,
                (NOW, NOW, json.dumps({"secret": "do-not-return"})),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  created_at, updated_at
                ) VALUES ('workstream-episode', 'repo-1', 'source-1',
                          'active', ?, ?)
                """,
                (NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at,
                  heartbeat_at
                ) VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (NOW, NOW),
            )
            conn.execute(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status,
                  first_seen_at, updated_at, metadata_json
                ) VALUES ('episode-1', 'workstream-episode', 'ci', 'head:base',
                          'head', 'base', 'queued', ?, ?, ?)
                """,
                (NOW, NOW, json.dumps({"token": "do-not-return"})),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle,
                  created_at, updated_at, metadata_json
                ) VALUES ('task-remediation', 'workstream-episode',
                          'ci_remediation', 'queued', ?, ?, ?)
                """,
                (
                    NOW,
                    NOW,
                    json.dumps({"remediation_episode_id": "episode-1"}),
                ),
            )

    def test_load_subject_snapshot_returns_authoritative_rows_and_ids_without_secrets(self):
        from robert_agent import workflow_operations

        snapshot = workflow_operations.load_subject_snapshot(
            self.db_path,
            "task",
            "task-1",
        )

        self.assertEqual(snapshot["subject_type"], "task")
        self.assertEqual(snapshot["task"]["task_id"], "task-1")
        self.assertEqual(snapshot["task"]["lifecycle"], "running")
        self.assertIsNone(snapshot["binding"])
        self.assertEqual(snapshot["lifecycle"]["workstream_id"], "workstream-1")
        self.assertEqual(snapshot["lifecycle"]["attempt_ids"], ["attempt-1"])
        self.assertNotIn("do-not-return", json.dumps(snapshot, sort_keys=True))

    def test_load_episode_snapshot_includes_episode_binding_and_child_task_ids(self):
        from robert_agent import workflow_operations

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            binding = workflow_store.bind_subject(
                conn,
                "remediation_episode",
                "episode-1",
                "pr_remediation",
                "dbos",
                NOW,
            )
            conn.execute(
                """
                UPDATE workflow_bindings
                SET metadata_json = ?
                WHERE subject_type = 'remediation_episode' AND subject_id = 'episode-1'
                """,
                (json.dumps({"token": "binding-secret", "reason": "test"}),),
            )

        snapshot = workflow_operations.load_subject_snapshot(
            self.db_path,
            "remediation_episode",
            "episode-1",
        )

        self.assertEqual(snapshot["episode"]["episode_id"], "episode-1")
        self.assertEqual(snapshot["binding"]["owner_engine"], binding["owner_engine"])
        self.assertEqual(snapshot["binding"]["metadata"], {"reason": "test"})
        self.assertIn("task-remediation", snapshot["lifecycle"]["task_ids"])
        rendered = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("do-not-return", rendered)
        self.assertNotIn("binding-secret", rendered)

    def test_claim_legacy_subject_is_idempotent_and_does_not_transfer_dbos_or_terminal(self):
        from robert_agent import workflow_operations

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            first = workflow_operations.claim_legacy_subject(
                conn,
                "task",
                "task-1",
                now=NOW,
            )
            same = workflow_operations.claim_legacy_subject(
                conn,
                "task",
                "task-1",
                now=NOW,
            )
            self.assertEqual(first, same)

            workflow_store.bind_subject(
                conn,
                "remediation_episode",
                "episode-1",
                "pr_remediation",
                "dbos",
                NOW,
            )
            self.assertIsNone(
                workflow_operations.claim_legacy_subject(
                    conn,
                    "remediation_episode",
                    "episode-1",
                    now=NOW,
                )
            )

            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed' WHERE task_id = 'task-1'"
            )
            self.assertEqual(
                workflow_operations.claim_legacy_subject(
                    conn,
                    "task",
                    "task-1",
                    now=NOW,
                )["owner_engine"],
                "legacy",
            )
            conn.execute(
                "UPDATE workflow_bindings SET binding_status = 'attention' "
                "WHERE subject_type = 'task' AND subject_id = 'task-1'"
            )
            self.assertIsNone(
                workflow_operations.claim_legacy_subject(
                    conn,
                    "task",
                    "task-1",
                    now=NOW,
                )
            )

    def test_claim_and_require_reject_invalid_subjects_and_owner_mismatches(self):
        from robert_agent import workflow_operations

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            with self.assertRaises(ValueError):
                workflow_operations.claim_legacy_subject(
                    conn,
                    "task",
                    "missing",
                    now=NOW,
                )
            with self.assertRaises(ValueError):
                workflow_operations.claim_legacy_subject(
                    conn,
                    "task",
                    "task-remediation",
                    now=NOW,
                )
            workflow_store.bind_subject(
                conn,
                "task",
                "task-1",
                "human_request",
                "legacy",
                NOW,
            )
            binding = workflow_operations.require_subject_owner(
                conn,
                "task",
                "task-1",
                "legacy",
            )
            self.assertEqual(binding["owner_engine"], "legacy")
            with self.assertRaises(ValueError):
                workflow_operations.require_subject_owner(
                    conn,
                    "task",
                    "task-1",
                    "dbos",
                )


if __name__ == "__main__":
    unittest.main()

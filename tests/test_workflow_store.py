from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage, execution_store
from tests import legacy_workflow_store as workflow_store


NOW = "2026-08-02T00:00:00Z"


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._insert_subjects()

    def _insert_subjects(self):
        self.conn.execute(
            """
            INSERT INTO repos(
              repo_id, full_name, github_account, default_base_branch,
              repo_root, worktree_root
            ) VALUES ('repo-1', 'example/repo', 'bot', 'main', '/repo', '/worktrees')
            """
        )
        self.conn.execute(
            """
            INSERT INTO workstreams(
              workstream_id, repo_id, lifecycle, created_at, updated_at
            ) VALUES ('workstream-1', 'repo-1', 'active', ?, ?)
            """,
            (NOW, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO tasks(
              task_id, workstream_id, task_kind, lifecycle, created_at, updated_at
            ) VALUES ('task-1', 'workstream-1', 'human_request', 'queued', ?, ?)
            """,
            (NOW, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO workstreams(
              workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
            ) VALUES ('workstream-waiting', 'repo-1', 'waiting_for_user',
                      'task-waiting', ?, ?)
            """,
            (NOW, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO tasks(
              task_id, workstream_id, task_kind, lifecycle, created_at, updated_at
            ) VALUES ('task-waiting', 'workstream-waiting', 'human_request',
                      'waiting_for_user', ?, ?)
            """,
            (NOW, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO tasks(
              task_id, workstream_id, task_kind, lifecycle, created_at, updated_at
            ) VALUES ('task-remediation', 'workstream-1', 'ci_remediation', 'completed', ?, ?)
            """,
            (NOW, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO pr_remediation_episodes(
              episode_id, workstream_id, episode_kind, subject_key,
              observed_head_sha, observed_base_sha, status, first_seen_at, updated_at
            ) VALUES ('episode-1', 'workstream-1', 'ci', 'example/repo!1',
                      'head', 'base', 'queued', ?, ?)
            """,
            (NOW, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO github_actions(
              action_id, task_id, action_type, created_at
            ) VALUES ('action-1', 'task-1', 'comment', ?)
            """,
            (NOW,),
        )
        self.conn.commit()

    def test_binding_is_idempotent_and_rejects_conflicts(self):
        first = workflow_store.bind_subject(
            self.conn, "task", "task-1", "human_request", "dbos", NOW
        )
        same = workflow_store.bind_subject(
            self.conn, "task", "task-1", "human_request", "dbos", NOW
        )

        self.assertEqual(first, same)
        self.assertEqual(first["workflow_id"], "robert:v1:task:task-1")
        with self.assertRaises(ValueError):
            workflow_store.bind_subject(
                self.conn, "task", "task-1", "human_request", "legacy", NOW
            )
        with self.assertRaises(ValueError):
            workflow_store.assert_owner(self.conn, "task", "task-1", "legacy")

    def test_schema_has_all_workflow_storage_tables_and_indexes(self):
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertTrue(
            {
                "workflow_bindings",
                "workflow_outbox",
                "workflow_signal_receipts",
                "workflow_shadow_observations",
                "publication_intents",
            }.issubset(tables)
        )
        self.assertTrue(
            {
                "idx_workflow_bindings_active",
                "idx_workflow_outbox_pending_due",
                "idx_workflow_shadow_observations_mismatch",
            }.issubset(indexes)
        )

    def test_only_human_tasks_and_remediation_episodes_can_bind(self):
        with self.assertRaises(ValueError):
            workflow_store.bind_subject(
                self.conn, "task", "task-remediation", "pr_remediation", "legacy", NOW
            )
        with self.assertRaises(ValueError):
            workflow_store.bind_subject(
                self.conn,
                "remediation_episode",
                "episode-1",
                "human_request",
                "legacy",
                NOW,
            )

        binding = workflow_store.ensure_legacy_binding(
            self.conn, "remediation_episode", "episode-1", "pr_remediation", NOW
        )
        self.assertEqual(binding["owner_engine"], "legacy")
        self.assertIsNone(binding["workflow_id"])

    def test_waiting_for_user_task_remains_eligible_for_legacy_migration(self):
        binding = workflow_store.ensure_legacy_binding(
            self.conn, "task", "task-waiting", "human_request", NOW
        )

        self.assertEqual(binding["binding_status"], "active")
        self.assertEqual(binding["owner_engine"], "legacy")



    def test_publication_intent_is_idempotent(self):
        first = execution_store.prepare_publication_intent(
            self.conn, "action-1", "publish:action-1", "request-1", NOW
        )
        same = execution_store.prepare_publication_intent(
            self.conn, "action-1", "publish:action-1", "request-1", NOW
        )

        self.assertEqual(first, same)
        with self.assertRaises(ValueError):
            execution_store.prepare_publication_intent(
                self.conn, "action-1", "publish:action-1", "different-request", NOW
            )
        published = execution_store.transition_publication_intent(
            self.conn, first["publication_id"], "executing", NOW
        )
        published = execution_store.transition_publication_intent(
            self.conn, first["publication_id"], "published", NOW
        )
        self.assertEqual("published", published["status"])

    def test_publication_intent_rejects_illegal_state_jumps(self):
        intent = execution_store.prepare_publication_intent(
            self.conn, "action-1", "publish:action-1", "request-1", NOW
        )

        with self.assertRaisesRegex(ValueError, "illegal publication intent transition"):
            execution_store.transition_publication_intent(
                self.conn, intent["publication_id"], "published", NOW
            )
        executing = execution_store.transition_publication_intent(
            self.conn, intent["publication_id"], "executing", NOW
        )
        absent = execution_store.transition_publication_intent(
            self.conn, executing["publication_id"], "confirmed_absent", NOW
        )
        retrying = execution_store.transition_publication_intent(
            self.conn, absent["publication_id"], "executing", NOW
        )
        attention = execution_store.transition_publication_intent(
            self.conn, retrying["publication_id"], "attention", NOW
        )
        with self.assertRaisesRegex(ValueError, "illegal publication intent transition"):
            execution_store.transition_publication_intent(
                self.conn, attention["publication_id"], "executing", NOW
            )

    def test_terminal_binding_cannot_be_reopened_as_attention(self):
        workflow_store.bind_subject(
            self.conn, "task", "task-1", "human_request", "legacy", NOW
        )
        workflow_store.close_binding(self.conn, "task", "task-1", NOW)

        with self.assertRaisesRegex(ValueError, "terminal"):
            workflow_store.mark_binding_attention(self.conn, "task", "task-1", NOW)

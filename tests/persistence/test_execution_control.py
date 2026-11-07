from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence.schema import database as storage
from robert_agent.persistence import execution_control as execution_store


NOW = "2026-08-02T00:00:00Z"


class ExecutionControlTests(unittest.TestCase):
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

    def test_binding_is_idempotent(self):
        first = execution_store.ensure_subject(self.conn, "task", "task-1", "human_request", NOW)
        same = execution_store.ensure_subject(self.conn, "task", "task-1", "human_request", "2026-08-02T01:00:00Z")

        self.assertEqual(first, same)

    def test_only_human_tasks_and_remediation_episodes_can_bind(self):
        with self.assertRaises(ValueError):
            execution_store.ensure_subject(self.conn, "task", "task-remediation", "pr_remediation", NOW)
        with self.assertRaises(ValueError):
            execution_store.ensure_subject(
                self.conn,
                "remediation_episode",
                "episode-1",
                "human_request",
                NOW,
            )

        binding = execution_store.ensure_subject(self.conn, "remediation_episode", "episode-1", "pr_remediation", NOW)
        self.assertEqual(binding["binding_status"], "active")
        self.assertEqual(binding["subject_id"], "episode-1")

    def test_waiting_for_user_task_can_bind(self):
        binding = execution_store.ensure_subject(self.conn, "task", "task-waiting", "human_request", NOW)

        self.assertEqual(binding["binding_status"], "active")

    def test_publication_intent_is_idempotent(self):
        first = execution_store.prepare_publication_intent(self.conn, "action-1", "publish:action-1", "request-1", NOW)
        same = execution_store.prepare_publication_intent(self.conn, "action-1", "publish:action-1", "request-1", NOW)

        self.assertEqual(first, same)
        with self.assertRaises(ValueError):
            execution_store.prepare_publication_intent(
                self.conn, "action-1", "publish:action-1", "different-request", NOW
            )
        published = execution_store.transition_publication_intent(self.conn, first["publication_id"], "executing", NOW)
        published = execution_store.transition_publication_intent(self.conn, first["publication_id"], "published", NOW)
        self.assertEqual("published", published["status"])

    def test_publication_intent_rejects_illegal_state_jumps(self):
        intent = execution_store.prepare_publication_intent(self.conn, "action-1", "publish:action-1", "request-1", NOW)

        with self.assertRaisesRegex(ValueError, "illegal publication intent transition"):
            execution_store.transition_publication_intent(self.conn, intent["publication_id"], "published", NOW)
        executing = execution_store.transition_publication_intent(self.conn, intent["publication_id"], "executing", NOW)
        absent = execution_store.transition_publication_intent(
            self.conn, executing["publication_id"], "confirmed_absent", NOW
        )
        retrying = execution_store.transition_publication_intent(self.conn, absent["publication_id"], "executing", NOW)
        attention = execution_store.transition_publication_intent(
            self.conn, retrying["publication_id"], "attention", NOW
        )
        with self.assertRaisesRegex(ValueError, "illegal publication intent transition"):
            execution_store.transition_publication_intent(self.conn, attention["publication_id"], "executing", NOW)

    def test_terminal_binding_cannot_be_reopened_as_attention(self):
        execution_store.ensure_subject(self.conn, "task", "task-1", "human_request", NOW)
        execution_store.close_binding(self.conn, "task", "task-1", NOW)

        with self.assertRaisesRegex(ValueError, "terminal"):
            execution_store.mark_binding_attention(self.conn, "task", "task-1", NOW)

"""Shared fixtures for work items tests."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class WorkItemCommandFixture(unittest.TestCase):
    def setUp(self):
        from robert_agent.adapters.sqlite import database as storage
        from robert_agent.work.application.commands import CommandContext

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "commands.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root, metadata_json
                ) VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees', '{}')
                """
            )
        self.context = CommandContext(
            actor_kind="operator",
            actor_identity="owner",
            allowed_repo_ids=frozenset({"repo-1"}),
            allowed_workers=frozenset({"default", "reviewer"}),
        )

    def _create(self, **overrides):
        from robert_agent.work.application import commands as work_items

        values = {
            "context": self.context,
            "repo_id": "repo-1",
            "title": "Add repository board",
            "description": "Build the control surface",
            "priority": "P1",
            "routing_mode": "auto",
            "requested_worker": None,
            "start": False,
            "idempotency_key": "create-1",
        }
        values.update(overrides)
        return work_items.create_work_item(self.db_path, **values)

    def _question(self, work_item_id, sequence):
        from robert_agent.work.application.commands import record_system_event

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            task_id, workstream_id = conn.execute(
                """
                SELECT w.active_task_id, wi.workstream_id
                FROM work_items wi
                JOIN workstreams w ON w.workstream_id = wi.workstream_id
                WHERE wi.work_item_id = ?
                """,
                (work_item_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, finished_at, metadata_json
                ) VALUES (?, ?, 1, 'completed', '2026-07-19T01:00:00+00:00', '{}')
                """,
                (f"attempt-{sequence}", task_id),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'waiting_for_user' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'waiting_for_user' WHERE workstream_id = ?",
                (workstream_id,),
            )
            event = record_system_event(
                conn,
                work_item_id,
                event_type="operator_question",
                idempotency_key=f"question-{sequence}",
                body=f"Question {sequence}?",
                metadata={"kind": "clarification", "task_id": task_id},
                now=f"2026-07-19T01:0{sequence}:00+00:00",
            )
        return event

    def _failed_github_task(self):
        now = "2026-10-19T00:00:00+00:00"
        payload = {
            "event_fingerprint": "comment:retry-source",
            "event_type": "comment",
            "source_key": "github:example/repo!21",
            "source_type": "pull_request",
            "number": 21,
            "title": "Update AIDev integration",
            "state": "open",
            "actor_login": "owner",
            "authorization_status": "accepted_context",
            "mentions_dd": True,
            "existing_pr_head_branch": "codex/dd-20-add-bkaidev-api-integration",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, html_url,
                  title, state, author_login
                ) VALUES (
                  'source-pr-21', 'repo-1', 'github:example/repo!21',
                  'pull_request', 21, 'https://github.com/example/repo/pull/21',
                  'Update AIDev integration', 'open', 'robot'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, author_association, authorization_status,
                  event_at, payload_json
                ) VALUES (
                  'event-retry-source', 'repo-1', 'source-pr-21',
                  'comment:retry-source', 'comment', 'owner', 'OWNER',
                  'accepted_context', ?, ?
                )
                """,
                (now, json.dumps(payload, sort_keys=True)),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  active_task_id, created_at, updated_at
                ) VALUES (
                  'github:example/repo!21', 'repo-1', 'source-pr-21',
                  'failed', NULL, ?, ?
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO work_items(
                  work_item_id, repo_id, title, priority, origin_type,
                  origin_source_id, routing_mode, workstream_id,
                  creation_idempotency_key, created_by, activated_at,
                  created_at, updated_at
                ) VALUES (
                  'wi-pr-21', 'repo-1', 'Update AIDev integration', 'P1',
                  'github', 'source-pr-21', 'auto', 'github:example/repo!21',
                  'backfill:github:example/repo!21', 'owner', ?, ?, ?
                )
                """,
                (now, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, route_id,
                  expected_output, created_at, updated_at
                ) VALUES (
                  'task-failed-github', 'github:example/repo!21', 'failed',
                  'P1', 'update-existing-pr', 'update_existing_pr', ?, ?
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-failed-github', 'event-retry-source', 'trigger', ?)
                """,
                (now,),
            )
        return "task-failed-github"

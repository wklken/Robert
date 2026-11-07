"""Shared fixtures for board tests."""

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path


class BoardReadModelFixture(unittest.TestCase):
    def setUp(self):
        from robert_agent.adapters.sqlite import database as storage
        from robert_agent.work.application import commands as work_items

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "board.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees')"
            )
        self.context = work_items.CommandContext(
            actor_kind="operator",
            actor_identity="owner",
            allowed_repo_ids=frozenset({"repo-1"}),
            allowed_workers=frozenset({"default"}),
        )

    def _create(self, name, *, start=False, priority="P2"):
        from robert_agent.work.application import commands as work_items

        return work_items.create_work_item(
            self.db_path,
            context=self.context,
            repo_id="repo-1",
            title=name,
            description=f"Requirement for {name}",
            priority=priority,
            routing_mode="auto",
            requested_worker=None,
            start=start,
            idempotency_key=f"create-{name}",
        )["item"]

    def _seed_six_columns(self):
        from robert_agent.work.application.commands import record_system_event

        backlog = self._create("Backlog card")
        todo = self._create("Todo card", start=True)
        doing = self._create("Doing card", start=True)
        waiting = self._create("Waiting card", start=True)
        review = self._create("Review card", start=True)
        done = self._create("Done card", start=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            now = "2026-07-19T02:00:00+00:00"
            doing_task = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (doing["workstream_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (doing_task,),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, branch_name, started_at) VALUES ('attempt-doing', ?, 1, 'running', 'codex/dd-doing', ?)",
                (doing_task, now),
            )

            waiting_task = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (waiting["workstream_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'waiting_for_user' WHERE task_id = ?",
                (waiting_task,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'waiting_for_user' WHERE workstream_id = ?",
                (waiting["workstream_id"],),
            )
            record_system_event(
                conn,
                waiting["work_item_id"],
                event_type="operator_question",
                idempotency_key="question-waiting",
                body="Choose the compatibility target.",
                metadata={"kind": "clarification"},
                now=now,
            )

            review_task = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (review["workstream_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?",
                (review_task,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL WHERE workstream_id = ?",
                (review["workstream_id"],),
            )
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('pr-review', 'repo-1', 'github:example/repo!12', 'pull_request', 12, 'https://github.com/example/repo/pull/12', 'Review PR', 'open')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, created_at, updated_at) VALUES ('github:example/repo!12', 'repo-1', 'pr-review', ?, 'completed', ?, ?)",
                (review["workstream_id"], now, now),
            )

            done_task = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (done["workstream_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?",
                (done_task,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL WHERE workstream_id = ?",
                (done["workstream_id"],),
            )
            conn.execute(
                "UPDATE work_items SET completed_at = ?, updated_at = ?, version = version + 1 WHERE work_item_id = ?",
                (now, now, done["work_item_id"]),
            )
        return {
            item["title"].split()[0].lower(): item
            for item in (backlog, todo, doing, waiting, review, done)
        }

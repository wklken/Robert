from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class BoardReadModelTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import storage, work_items

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
        from robert_agent import work_items

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
        from robert_agent.work_items import record_system_event

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
            conn.execute("UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?", (doing_task,))
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, branch_name, started_at) VALUES ('attempt-doing', ?, 1, 'running', 'codex/dd-doing', ?)",
                (doing_task, now),
            )

            waiting_task = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (waiting["workstream_id"],),
            ).fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle = 'waiting_for_user' WHERE task_id = ?", (waiting_task,))
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
            conn.execute("UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?", (review_task,))
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
            conn.execute("UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?", (done_task,))
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL WHERE workstream_id = ?",
                (done["workstream_id"],),
            )
            conn.execute(
                "UPDATE work_items SET completed_at = ?, updated_at = ?, version = version + 1 WHERE work_item_id = ?",
                (now, now, done["work_item_id"]),
            )
        return {item["title"].split()[0].lower(): item for item in (backlog, todo, doing, waiting, review, done)}

    def test_projection_places_each_item_in_exactly_one_of_six_columns(self):
        from robert_agent import board

        self._seed_six_columns()
        result = board.list_board(self.db_path)

        self.assertEqual(
            [column["id"] for column in result["columns"]],
            ["backlog", "todo", "doing", "waiting", "review", "done"],
        )
        self.assertEqual({column["count"] for column in result["columns"]}, {1})
        by_title = {item["title"]: item["column"] for item in result["items"]}
        self.assertEqual(
            by_title,
            {
                "Backlog card": "backlog",
                "Todo card": "todo",
                "Doing card": "doing",
                "Waiting card": "waiting",
                "Review card": "review",
                "Done card": "done",
            },
        )

    def test_resolved_old_attention_does_not_return_card_to_waiting(self):
        from robert_agent import board
        from robert_agent.work_items import record_system_event

        item = self._create("Resolved card", start=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            question = record_system_event(
                conn,
                item["work_item_id"],
                event_type="publication_failed",
                idempotency_key="failure-1",
                body="Publish failed.",
            )
            record_system_event(
                conn,
                item["work_item_id"],
                event_type="retry_requested",
                idempotency_key="resolve-1",
                resolves_event_id=question["event_id"],
            )

        result = board.list_board(self.db_path)
        self.assertEqual(result["items"][0]["column"], "todo")
        self.assertEqual(result["items"][0]["attention_signals"], [])

    def test_unmerged_closed_pr_remains_in_review_with_attention(self):
        from robert_agent import board
        from robert_agent.work_items import record_system_event

        item = self._create("Closed PR card", start=True)
        now = "2026-07-19T03:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            task_id = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
                (item["workstream_id"],),
            ).fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?", (task_id,))
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL WHERE workstream_id = ?",
                (item["workstream_id"],),
            )
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('pr-closed', 'repo-1', 'github:example/repo!44', 'pull_request', 44, 'https://github.com/example/repo/pull/44', 'Closed PR', 'closed')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, created_at, updated_at) VALUES ('github:example/repo!44', 'repo-1', 'pr-closed', ?, 'completed', ?, ?)",
                (item["workstream_id"], now, now),
            )
            record_system_event(
                conn,
                item["work_item_id"],
                event_type="unmerged_pr_closed",
                idempotency_key="closed-pr-44",
                body="PR #44 closed without merge.",
                now=now,
            )

        projected = board.list_board(self.db_path)["items"][0]
        self.assertEqual(projected["column"], "review")
        self.assertEqual(projected["reason_code"], "unmerged_pr_closed")
        self.assertIn("request_changes", projected["valid_commands"])

    def test_direct_closed_pr_stays_in_review_until_operator_cancels(self):
        from robert_agent import board, run_once, work_items

        now = "2026-10-13T06:35:18+00:00"
        source_key = "github:example/repo!3353"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('pr-direct', 'repo-1', ?, 'pull_request', 3353, 'https://github.com/example/repo/pull/3353', 'Direct PR', 'open')",
                (source_key,),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, 'repo-1', 'pr-direct', 'active', ?, ?)",
                (source_key, now, now),
            )
            item = work_items.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="pr-direct",
                workstream_id=source_key,
                actor_identity="owner",
                route_confidence="high",
                now=now,
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at) VALUES ('task-direct', ?, 'completed', 'P1', 'review', 'review', ?, ?)",
                (source_key, now, now),
            )
            run_once._record_remote_terminal_state(
                conn,
                "repo-1",
                source_key,
                "pr-direct",
                source_key,
                "pull_request",
                3353,
                "task-direct",
                {
                    "state": "closed",
                    "merged": False,
                    "closed_at": "2026-10-13T06:30:26Z",
                },
                "canceled",
                "remote_pr_closed",
                now,
            )

        projected = board.list_board(self.db_path)["items"][0]
        self.assertEqual(projected["column"], "review")
        self.assertEqual(projected["reason_code"], "unmerged_pr_closed")
        self.assertEqual(projected["pr"]["number"], 3353)
        self.assertEqual(projected["pr"]["state"], "closed")
        self.assertEqual(projected["pr"]["url"], "https://github.com/example/repo/pull/3353")
        self.assertIn("cancel", projected["valid_commands"])

        canceled = work_items.execute_command(
            self.db_path,
            item["work_item_id"],
            context=self.context,
            command="cancel",
            expected_version=projected["version"],
            idempotency_key="cancel-direct-closed-pr",
        )
        self.assertTrue(canceled["ok"])
        archived = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(archived["column"], "history")
        self.assertEqual(archived["valid_commands"], [])
        self.assertEqual(board.list_board(self.db_path)["items"], [])

    def test_projection_does_not_expose_non_http_pull_request_urls(self):
        from robert_agent import board

        items = self._seed_six_columns()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_sources SET html_url = 'javascript:alert(1)' WHERE source_id = 'pr-review'"
            )

        detail = board.get_work_item_detail(
            self.db_path,
            items["review"]["work_item_id"],
        )
        self.assertIsNone(detail["pr"]["url"])
        self.assertNotIn("javascript:", json.dumps(detail))

    def test_filters_cursor_detail_and_timeline_are_safe(self):
        from robert_agent import board

        for index in range(55):
            self._create(f"Card {index:02d}", priority="P1" if index % 2 else "P2")
        first = board.list_board(self.db_path, repo="repo-1", priority="P1", limit=10)
        second = board.list_board(
            self.db_path,
            repo="repo-1",
            priority="P1",
            limit=10,
            cursor=first["next_cursor"],
        )
        first_ids = {item["work_item_id"] for item in first["items"]}
        second_ids = {item["work_item_id"] for item in second["items"]}
        detail = board.get_work_item_detail(self.db_path, first["items"][0]["work_item_id"])
        timeline = board.list_work_item_events(
            self.db_path,
            first["items"][0]["work_item_id"],
            limit=10,
        )
        serialized = json.dumps({"first": first, "detail": detail, "timeline": timeline})

        self.assertFalse(first_ids & second_ids)
        self.assertTrue(all(item["priority"] == "P1" for item in first["items"]))
        self.assertIn("edit", detail["valid_commands"])
        for forbidden in ("metadata_json", "payload_json", "failure_json", "error_json", "csrf_token"):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaises(board.BoardQueryError):
            board.list_board(self.db_path, cursor="not-a-cursor")


if __name__ == "__main__":
    unittest.main()

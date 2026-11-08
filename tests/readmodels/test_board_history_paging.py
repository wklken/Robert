from contextlib import closing
import sqlite3
from unittest.mock import patch

from robert_agent.readmodels import board
from robert_agent.work.application.commands import record_system_event
from tests.support.board import BoardReadModelFixture


OLDER = "2026-07-19T00:00:00Z"
NEWER = "2026-07-19T01:00:00Z"
NEWEST = "2026-07-19T02:00:00Z"


class BoardHistoryPagingTests(BoardReadModelFixture):
    def test_history_partition_uses_one_read_snapshot_during_completion(self):
        self._seed_six_columns()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        before = board.list_board(self.db_path)
        target = next(item["work_item_id"] for item in before["items"] if item["column"] == "doing")
        original = board.work_item_queries.terminal_board_page

        def complete(*args, **kwargs):
            page = original(*args, **kwargs)
            with closing(sqlite3.connect(self.db_path)) as writer, writer:
                writer.execute("UPDATE work_items SET completed_at=? WHERE work_item_id=?", (NEWEST, target))
            return page

        with patch.object(board.work_item_queries, "terminal_board_page", side_effect=complete):
            during = board.list_board(self.db_path)
        self.assertCountEqual(self.item_ids(during), self.item_ids(before))
        self.assertEqual(during["counts"], before["counts"])
        after = board.list_board(self.db_path)
        self.assertEqual(next(item["column"] for item in after["items"] if item["work_item_id"] == target), "done")

    def add_terminal_item(
        self,
        conn,
        item_id,
        updated,
        *,
        canceled=False,
        worker=None,
        branch="codex/history",
        priority="P1",
        repo="repo-1",
    ):
        stream_id = f"stream:{item_id}"
        task_id = f"task:{item_id}"
        lifecycle = "canceled" if canceled else "completed"
        conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES (?,?,?,?,?)",
            (stream_id, repo, lifecycle, OLDER, updated),
        )
        conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,routing_mode,requested_worker,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (task_id, stream_id, lifecycle, "manual" if worker else "auto", worker, OLDER, updated),
        )
        conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,branch_name,finished_at) VALUES (?,?,1,?,?,?)",
            (f"attempt:{item_id}", task_id, lifecycle, branch, updated),
        )
        conn.execute(
            "INSERT INTO work_items(work_item_id,repo_id,title,priority,origin_type,workstream_id,creation_idempotency_key,"
            "created_by,activated_at,created_at,updated_at,completed_at,canceled_at)"
            " VALUES (?,?,?,?,'web',?,?,'owner',?,?,?,?,?)",
            (
                item_id,
                repo,
                f"Finished work {item_id}",
                priority,
                stream_id,
                f"create-{item_id}",
                OLDER,
                OLDER,
                updated,
                None if canceled else updated,
                updated if canceled else None,
            ),
        )

    def seed_filter_history(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
                " VALUES ('repo-2','example/other','robot','main','/other','/other-trees')"
            )
            self.add_terminal_item(conn, "item-60", NEWER, worker="latest-worker")
            self.add_terminal_item(conn, "item-20", NEWER, canceled=True, worker="archived-worker")
            self.add_terminal_item(conn, "item-30", OLDER)
            self.add_terminal_item(conn, "item-10", OLDER, worker="deep-worker", branch="codex/cache-only-needle")
            self.add_terminal_item(conn, "item-50", NEWEST, worker="low-priority", priority="P2")
            self.add_terminal_item(conn, "item-40", NEWEST, worker="other-repo", repo="repo-2")
            failure = record_system_event(
                conn, "item-60", event_type="publication_failed", idempotency_key="resolved-failure", now=NEWER
            )
            record_system_event(
                conn,
                "item-60",
                event_type="retry_requested",
                idempotency_key="resolved-retry",
                resolves_event_id=failure["event_id"],
                now=NEWER,
            )
            record_system_event(
                conn, "item-10", event_type="publication_failed", idempotency_key="unresolved-failure", now=OLDER
            )

    def item_ids(self, page):
        return [item["work_item_id"] for item in page["items"]]

    def test_default_page_bounds_full_card_projection(self):
        self._seed_six_columns()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index in range(211):
                self.add_terminal_item(conn, f"completed-{index:03d}", NEWEST)

        with patch.object(board, "_project_item", wraps=board._project_item) as project:
            page = board.list_board(self.db_path, limit=7)

        self.assertEqual(len(page["items"]), 7)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(
            page["counts"],
            {"backlog": 1, "todo": 1, "doing": 1, "waiting": 1, "review": 1, "done": 212, "history": 0},
        )
        # Five active cards may need lifecycle evaluation, plus one page and its lookahead.
        self.assertLessEqual(project.call_count, 5 + 7 + 1)

    def test_terminal_pages_preserve_order_counts_and_agent_filters(self):
        self.seed_filter_history()
        filters = {"repo": "example/repo", "priority": "P1"}
        first = board.list_board(self.db_path, **filters, include_history=True, limit=2)
        self.assertEqual(self.item_ids(first), ["item-60", "item-20"])
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["filters"]["agents"], ["Auto", "archived-worker", "deep-worker", "latest-worker"])
        second = board.list_board(self.db_path, **filters, include_history=True, limit=2, cursor=first["next_cursor"])
        self.assertEqual(self.item_ids(second), ["item-30", "item-10"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["filters"]["agents"], first["filters"]["agents"])
        counts = {"backlog": 0, "todo": 0, "doing": 0, "waiting": 0, "review": 0, "done": 3, "history": 1}
        self.assertEqual(first["counts"], counts)
        self.assertEqual(second["counts"], counts)

        done = board.list_board(self.db_path, **filters, include_history=True, column="done", limit=1)
        self.assertEqual(self.item_ids(done), ["item-60"])
        self.assertEqual(done["counts"], counts)
        self.assertEqual(done["filters"]["agents"], first["filters"]["agents"])
        archived = board.list_board(self.db_path, **filters, include_history=True, column="history")
        self.assertEqual(self.item_ids(archived), ["item-20"])
        visible = board.list_board(self.db_path, **filters)
        self.assertEqual(self.item_ids(visible), ["item-60", "item-30", "item-10"])
        self.assertEqual(visible["counts"]["history"], 0)

    def test_terminal_filters_use_current_task_branch_agent_and_unresolved_attention(self):
        self.seed_filter_history()
        filters = {"repo": "repo-1", "priority": "P1", "include_history": True}
        by_branch = board.list_board(self.db_path, **filters, query="CACHE-ONLY-NEEDLE")
        self.assertEqual(self.item_ids(by_branch), ["item-10"])
        self.assertEqual(by_branch["items"][0]["branch"], "codex/cache-only-needle")
        by_agent = board.list_board(self.db_path, **filters, agent="deep-worker")
        self.assertEqual(self.item_ids(by_agent), ["item-10"])
        self.assertEqual(by_agent["items"][0]["agent"], "deep-worker")
        needs_attention = board.list_board(self.db_path, **filters, attention=True)
        self.assertEqual(self.item_ids(needs_attention), ["item-10"])
        self.assertEqual(needs_attention["items"][0]["attention_signals"][0]["type"], "publication_failed")
        clear = board.list_board(self.db_path, **filters, attention=False)
        self.assertEqual(self.item_ids(clear), ["item-60", "item-20", "item-30"])

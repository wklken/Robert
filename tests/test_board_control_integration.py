from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class BoardControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import storage, work_items

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "dd.sqlite3"
        self.repo_id = "repo:example/repo"
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "data_dir": str(self.data_dir),
                    "database": "dd.sqlite3",
                    "max_concurrency": 2,
                    "workers": [
                        {
                            "name": "default",
                            "agent": "cbc",
                            "command": "cbc",
                            "default_model": "default-model",
                            "default_effort": "medium",
                        },
                        {
                            "name": "reviewer",
                            "agent": "codex",
                            "command": "codex",
                            "default_model": "review-model",
                            "default_effort": "high",
                        },
                    ],
                    "repos": [
                        {
                            "full_name": "example/repo",
                            "github_account": "robot",
                            "trusted_actors": ["owner"],
                            "default_base_branch": "main",
                            "repo_root": str(self.repo_root),
                            "worktree_root": str(self.worktree_root),
                            "max_concurrency": 2,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.empty_fixture = self.root / "empty.json"
        self.empty_fixture.write_text('{"events": []}', encoding="utf-8")
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, 'example/repo', 'robot', 'main', ?, ?)",
                (self.repo_id, str(self.repo_root), str(self.worktree_root)),
            )
        self.context = work_items.CommandContext(
            actor_kind="operator",
            actor_identity="owner",
            allowed_repo_ids=frozenset({self.repo_id}),
            allowed_workers=frozenset({"default", "reviewer"}),
        )

    def _run_local_cycle(self):
        from robert_agent import run_once
        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.empty_fixture,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        return result

    def _finish_task(self, task_id, now):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status = 'completed', finished_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )

    def test_local_backlog_waiting_review_and_merge_lifecycle(self):
        from robert_agent import run_once
        from robert_agent import board, work_items

        created = work_items.create_work_item(
            self.db_path,
            context=self.context,
            repo_id=self.repo_id,
            title="Fix repository setting bug",
            description="Fix the repository setting bug.",
            priority="P1",
            routing_mode="auto",
            requested_worker=None,
            start=False,
            idempotency_key="accept-create-auto",
        )["item"]
        edited = work_items.execute_command(
            self.db_path,
            created["work_item_id"],
            context=self.context,
            command="edit",
            expected_version=created["version"],
            idempotency_key="accept-edit-auto",
            description="Fix the repository setting bug with a regression test.",
        )["item"]
        started = work_items.execute_command(
            self.db_path,
            created["work_item_id"],
            context=self.context,
            command="start",
            expected_version=edited["version"],
            idempotency_key="accept-start-auto",
        )
        manual = work_items.create_work_item(
            self.db_path,
            context=self.context,
            repo_id=self.repo_id,
            title="Fix repository validation bug",
            description="Fix the repository validation bug with the named reviewer worker.",
            priority="P2",
            routing_mode="manual",
            requested_worker="reviewer",
            start=True,
            idempotency_key="accept-create-manual",
        )

        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            prepared = conn.execute(
                """
                SELECT t.task_id, t.routing_mode, t.requested_worker,
                       a.worktree_path, a.branch_name
                FROM tasks t JOIN attempts a ON a.task_id = t.task_id
                ORDER BY t.created_at, t.task_id
                """
            ).fetchall()
        self.assertEqual(len(prepared), 2)
        self.assertEqual({row[1] for row in prepared}, {"auto", "manual"})
        self.assertIn("reviewer", {row[2] for row in prepared})
        self.assertEqual(len({row[3] for row in prepared}), 2)
        self.assertLessEqual(board.list_board(self.db_path)["capacity"]["running"], 2)

        root_task_id = started["task_id"]
        root_attempt = next(row for row in prepared if row[0] == root_task_id)
        now = "2026-07-19T04:00:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE attempts SET status = 'completed', finished_at = ? WHERE task_id = ?",
                (now, root_task_id),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'waiting_for_user', updated_at = ? WHERE task_id = ?",
                (now, root_task_id),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'waiting_for_user', updated_at = ? WHERE workstream_id = ?",
                (now, started["item"]["workstream_id"]),
            )
            work_items.record_system_event(
                conn,
                created["work_item_id"],
                event_type="operator_question",
                idempotency_key="accept-question",
                body="Should the setting remain backward compatible?",
                metadata={"kind": "clarification"},
                now=now,
            )
        waiting = board.get_work_item_detail(self.db_path, created["work_item_id"])
        self.assertEqual(waiting["column"], "waiting")

        replied = work_items.execute_command(
            self.db_path,
            created["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=waiting["version"],
            idempotency_key="accept-reply",
            body="Keep backward compatibility.",
        )
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            attempts = conn.execute(
                """
                SELECT t.task_id, t.parent_task_id, a.worktree_path, a.branch_name
                FROM tasks t JOIN attempts a ON a.task_id = t.task_id
                WHERE t.workstream_id = ? ORDER BY t.created_at, t.task_id
                """,
                (started["item"]["workstream_id"],),
            ).fetchall()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1][1], root_task_id)
        self.assertEqual(attempts[1][2:], root_attempt[3:5])

        fix_task_id = replied["task_id"]
        self._finish_task(fix_task_id, "2026-07-19T04:10:00+00:00")
        pr_workstream_id = "github:example/repo!81"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL WHERE workstream_id = ?",
                (started["item"]["workstream_id"],),
            )
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state) VALUES ('source-pr-81', ?, ?, 'pull_request', 81, 'https://github.com/example/repo/pull/81', 'Repository setting', 'open')",
                (self.repo_id, pr_workstream_id),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'source-pr-81', ?, 'completed', ?, ?)",
                (pr_workstream_id, self.repo_id, started["item"]["workstream_id"], now, now),
            )
            work_items.record_system_event(
                conn,
                created["work_item_id"],
                event_type="pr_opened",
                idempotency_key="accept-pr-opened",
                metadata={"source_key": pr_workstream_id, "pr_number": 81},
                now=now,
            )
        review = board.get_work_item_detail(self.db_path, created["work_item_id"])
        self.assertEqual(review["column"], "review")
        self.assertEqual(review["pr"]["number"], 81)

        changes = work_items.execute_command(
            self.db_path,
            created["work_item_id"],
            context=self.context,
            command="request_changes",
            expected_version=review["version"],
            idempotency_key="accept-review-fix",
            body="Add a compatibility regression test.",
        )
        self._run_local_cycle()
        self._finish_task(changes["task_id"], "2026-07-19T04:20:00+00:00")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            run_once._record_remote_terminal_state(
                conn,
                self.repo_id,
                pr_workstream_id,
                "source-pr-81",
                pr_workstream_id,
                "pull_request",
                81,
                changes["task_id"],
                {
                    "state": "closed",
                    "merged": True,
                    "merged_at": "2026-07-19T04:30:00+00:00",
                    "html_url": "https://github.com/example/repo/pull/81",
                },
                "completed",
                "remote_pr_merged",
                "2026-07-19T04:30:00+00:00",
            )
        done = board.get_work_item_detail(self.db_path, created["work_item_id"])
        self.assertEqual(done["column"], "done")
        event_types = [event["event_type"] for event in done["events"]]
        for event_type in (
            "created",
            "edited",
            "activated",
            "operator_question",
            "user_response",
            "pr_opened",
            "changes_requested",
            "pr_merged",
        ):
            self.assertIn(event_type, event_types)
        self.assertEqual(manual["item"]["requested_worker"], "reviewer")

    def test_fake_worker_publish_and_github_dedup_project_one_review_item(self):
        from robert_agent import controlled_e2e_acceptance
        from robert_agent import board

        workspace = self.root / "controlled"
        result = controlled_e2e_acceptance.controlled_e2e_acceptance(
            self.config_path,
            workspace_dir=workspace,
            timeout_seconds=5,
            poll_interval_seconds=0.05,
        )
        self.assertTrue(result["ok"], result)
        projected = board.list_board(result["db_path"])
        self.assertEqual(len(projected["items"]), 1)
        self.assertEqual(projected["items"][0]["column"], "review")
        self.assertEqual(projected["items"][0]["pr"]["number"], 42)
        with closing(sqlite3.connect(result["db_path"])) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM work_item_events WHERE event_type = 'pr_opened'"
                ).fetchone()[0],
                1,
            )

    def test_no_pr_results_and_existing_operator_routes_remain_available(self):
        from robert_agent import audit_result
        from robert_agent.worker import result
        from robert_agent import web
        base = {
            "task_id": "task-local",
            "attempt_id": "attempt-local",
            "output_type": "local_result",
            "planned_github_actions": [],
            "consumed_event_fingerprints": [],
            "consumed_work_item_event_ids": ["wie-local"],
            "verification": [],
            "used_skills": [],
        }
        analysis = result.build_result(**base, handoff="Analysis completed without code changes.")
        no_op = result.build_result(**base, handoff="Code change is unnecessary; existing behavior is correct.")
        for payload in (analysis, no_op):
            audited = audit_result.audit_result(
                payload,
                allowed_github_actions=[],
                expected_output="local_result",
                origin_type="web",
            )
            self.assertEqual(audited["status"], "accepted")

        board_status, _headers, board_body = web.build_http_response("/board", self.db_path)
        operations_status, _headers, operations_body = web.build_http_response(
            "/operations", self.db_path
        )
        artifact_status, _headers, _body = web.build_http_response(
            "/artifact.txt?task_id=missing&artifact_type=prompt", self.db_path
        )
        knowledge_status, _headers, knowledge_body = web.handle_dashboard_post(
            "/knowledge/propose", b"", self.db_path
        )
        self.assertEqual(board_status, 200)
        self.assertIn(b'id="board-app"', board_body)
        self.assertEqual(operations_status, 200)
        self.assertIn(b'id="dashboard-app"', operations_body)
        self.assertEqual(artifact_status, 404)
        self.assertEqual(knowledge_status, 400)
        self.assertIn(b"repo_id is required", knowledge_body)


if __name__ == "__main__":
    unittest.main()

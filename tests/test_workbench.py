from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import storage

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "dd.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                ) VALUES ('repo-1', 'Org/repo', 'dd-bot', 'main', '/repo', '/repo/.worktrees')
                """
            )

    def seed_workstream(
        self,
        suffix="1",
        *,
        task_lifecycle="running",
        workstream_lifecycle="active",
        title=None,
        author="wklken",
        priority="P1",
        updated_at=None,
    ):
        source_id = f"source-{suffix}"
        source_key = f"github:Org/repo#{suffix}"
        workstream_id = f"workstream-{suffix}"
        task_id = f"task-{suffix}"
        event_id = f"event-{suffix}"
        updated_at = updated_at or f"2026-07-19T09:{int(suffix) % 60:02d}:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  html_url, title, state, author_login, source_updated_at
                ) VALUES (?, 'repo-1', ?, 'pull_request', ?, ?, ?, 'open', ?, ?)
                """,
                (
                    source_id,
                    source_key,
                    int(suffix),
                    f"https://github.com/Org/repo/pull/{suffix}",
                    title or f"Improve workbench {suffix}",
                    author,
                    updated_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  active_task_id, created_at, updated_at
                ) VALUES (?, 'repo-1', ?, ?, ?, ?, ?)
                """,
                (
                    workstream_id,
                    source_id,
                    workstream_lifecycle,
                    task_id if task_lifecycle in {"detected", "authorized", "classified", "queued", "running"} else None,
                    updated_at,
                    updated_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, route_id,
                  expected_output, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'review-pr', 'review_report', ?, ?)
                """,
                (task_id, workstream_id, task_lifecycle, priority, updated_at, updated_at),
            )
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint,
                  event_type, actor_login, authorization_status, event_at
                ) VALUES (?, 'repo-1', ?, ?, 'review_requested', ?, 'authorized', ?)
                """,
                (event_id, source_id, f"fingerprint-{suffix}", author, updated_at),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES (?, ?, 'trigger', ?)
                """,
                (task_id, event_id, updated_at),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, worktree_path,
                  branch_name, started_at, heartbeat_at, finished_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"attempt-{suffix}",
                    task_id,
                    "running" if task_lifecycle == "running" else "completed",
                    f"/repo/.worktrees/task-{suffix}",
                    f"codex/task-{suffix}",
                    updated_at,
                    updated_at,
                    None if task_lifecycle == "running" else updated_at,
                ),
            )
        return workstream_id, task_id

    def seed_publish_action(
        self,
        task_id="task-1",
        *,
        audit_status="accepted",
        publish_status="skipped",
        metadata=None,
    ):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_actions(
                  action_id, task_id, action_type, target_url, audit_status,
                  publish_status, created_at, metadata_json
                ) VALUES ('action-1', ?, 'review_comment', ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    "https://github.com/Org/repo/pull/1",
                    audit_status,
                    publish_status,
                    "2026-07-19T09:59:00+00:00",
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def seed_notification(self, notification_type, task_id="task-1"):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, status, created_at
                ) VALUES ('notification-1', ?, ?, 'recorded', '2026-06-01T00:00:00+00:00')
                """,
                (task_id, notification_type),
            )

    def test_failed_publish_action_is_attention(self):
        from robert_agent import workbench

        self.seed_workstream(task_lifecycle="completed", workstream_lifecycle="active")
        self.seed_publish_action(
            metadata={
                "publish": {
                    "status": "publish_failed",
                    "safe_error": "gh failed",
                }
            },
        )

        payload = workbench.list_work_items(self.db_path)

        self.assertEqual(payload["items"][0]["bucket"], "needs_attention")
        self.assertEqual(payload["items"][0]["reason_code"], "publish_failed")
        self.assertEqual(payload["counts"]["needs_attention"], 1)

    def test_old_notification_does_not_create_attention(self):
        from robert_agent import workbench

        self.seed_workstream(task_lifecycle="completed", workstream_lifecycle="completed")
        self.seed_notification("worker_startup_failed")

        payload = workbench.list_work_items(self.db_path, bucket="history")

        self.assertEqual(payload["items"][0]["bucket"], "history")
        self.assertEqual(payload["counts"]["needs_attention"], 0)

        active_payload = workbench.list_work_items(self.db_path)
        self.assertEqual(active_payload["items"], [])
        self.assertEqual(active_payload["counts"]["history"], 1)

    def test_search_qualifiers_and_cursor_cover_the_full_database(self):
        from robert_agent import workbench

        for index in range(1, 36):
            self.seed_workstream(str(index), title=f"Workbench item {index}")

        first = workbench.list_work_items(
            self.db_path,
            query="repo:Org/repo is:active task:task-",
            limit=30,
        )
        second = workbench.list_work_items(
            self.db_path,
            query="repo:Org/repo is:active task:task-",
            limit=30,
            cursor=first["next_cursor"],
        )

        self.assertEqual(len(first["items"]), 30)
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(len(second["items"]), 5)
        self.assertTrue(
            {item["id"] for item in first["items"]}.isdisjoint(
                item["id"] for item in second["items"]
            )
        )

    def test_detail_returns_structured_safe_data(self):
        from robert_agent import workbench

        workstream_id, task_id = self.seed_workstream(
            task_lifecycle="completed",
            workstream_lifecycle="completed",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO worker_results(
                  result_id, task_id, attempt_id, output_type,
                  verification_json, handoff, created_at, metadata_json
                ) VALUES (
                  'result-1', ?, 'attempt-1', 'review_report',
                  '[{"command":"python3 -m unittest","status":"passed"}]',
                  'Review completed', '2026-07-19T10:00:00+00:00',
                  '{"audit":{"status":"accepted"},"secret":"must-not-leak"}'
                )
                """,
                (task_id,),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, created_at
                ) VALUES (
                  'phase-1', 'attempt-1', 'verify', 'completed',
                  'Focused tests passed', '2026-07-19T09:58:00+00:00'
                )
                """
            )

        detail = workbench.get_work_item(self.db_path, workstream_id)
        encoded = json.dumps(detail, sort_keys=True)

        self.assertEqual(detail["id"], workstream_id)
        self.assertEqual(detail["tasks"][0]["task_id"], task_id)
        self.assertEqual(detail["results"][0]["audit_status"], "accepted")
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("metadata_json", encoded)
        self.assertIsNone(workbench.get_work_item(self.db_path, "missing"))

    def test_web_routes_workbench_list_and_detail(self):
        from robert_agent import web
        workstream_id, _task_id = self.seed_workstream()

        status, headers, body = web.build_http_response(
            "/api/work-items?bucket=working&repo=Org%2Frepo&limit=20",
            self.db_path,
        )
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(payload["items"][0]["id"], workstream_id)

        status, headers, body = web.build_http_response(
            f"/api/work-items/{workstream_id}",
            self.db_path,
        )
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(payload["id"], workstream_id)

    def test_web_returns_safe_workbench_errors(self):
        from robert_agent import web
        self.seed_workstream()

        for path in (
            "/api/work-items?bucket=invalid",
            "/api/work-items?limit=101",
            "/api/work-items?cursor=invalid",
        ):
            status, headers, body = web.build_http_response(path, self.db_path)
            payload = json.loads(body)
            self.assertEqual(status, 400)
            self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["safe_error"])

        status, _headers, body = web.build_http_response(
            "/api/work-items/missing",
            self.db_path,
        )
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["safe_error"], "work item not found")

    def test_web_returns_safe_error_for_incomplete_schema(self):
        from robert_agent import web
        old_db = self.root / "old.sqlite3"
        with closing(sqlite3.connect(old_db)) as conn, conn:
            conn.execute("CREATE TABLE repos(repo_id TEXT PRIMARY KEY)")

        status, headers, body = web.build_http_response(
            "/api/work-items",
            old_db,
        )

        self.assertEqual(status, 409)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertIn("database schema", json.loads(body)["safe_error"])


if __name__ == "__main__":
    unittest.main()

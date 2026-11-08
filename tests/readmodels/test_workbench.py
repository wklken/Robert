from robert_agent.readmodels import dashboard
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        from robert_agent.persistence.schema import database as storage

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
                    task_id
                    if task_lifecycle in {"detected", "authorized", "classified", "queued", "running"}
                    else None,
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

    def test_history_exposes_board_lifecycle_without_changing_workflow_bucket(self):
        from robert_agent.readmodels import board
        from robert_agent.readmodels import workbench

        self.seed_workstream(task_lifecycle="completed", workstream_lifecycle="completed")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE github_sources SET state = 'closed'")
            conn.execute("""
                INSERT INTO work_items(
                  work_item_id, repo_id, title, origin_type, origin_source_id,
                  workstream_id, creation_idempotency_key, canceled_at, created_by, created_at, updated_at
                ) VALUES ('wi-1', 'repo-1', 'Canceled item', 'github', 'source-1',
                          'workstream-1', 'create-1', '2026-07-20T00:00:00Z', 'tester',
                          '2026-07-19T00:00:00Z', '2026-07-20T00:00:00Z')
            """)
        history = workbench.list_work_items(self.db_path, bucket="history")["items"][0]
        detail = workbench.get_work_item(self.db_path, "workstream-1")
        expected = board.get_work_item_detail(self.db_path, "wi-1")
        self.assertEqual(history["bucket"], "history")
        self.assertEqual(history["workstream_lifecycle"], "completed")
        for item in (history, detail):
            self.assertEqual(item["lifecycle"]["column"], expected["column"])
            self.assertEqual(item["lifecycle"]["reason_code"], "canceled")
            self.assertEqual(item["lifecycle"]["work_item_id"], "wi-1")

    def test_workflow_without_board_item_does_not_invent_delivery(self):
        from robert_agent.readmodels import workbench

        self.seed_workstream(task_lifecycle="completed", workstream_lifecycle="completed")
        item = workbench.list_work_items(self.db_path, bucket="history")["items"][0]
        self.assertIsNone(item["lifecycle"])
        self.assertEqual(item["workstream_lifecycle"], "completed")

    def test_operations_task_link_falls_back_to_workstream_source(self):

        self.seed_workstream()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM task_events WHERE task_id = 'task-1'")
        task = dashboard.build_dashboard_data(self.db_path)["recent_tasks"][0]
        self.assertEqual(task["github_url"], "https://github.com/Org/repo/pull/1")
        self.assertEqual(task["source_number"], 1)

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
        from robert_agent.readmodels import workbench

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
        from robert_agent.readmodels import workbench

        self.seed_workstream(task_lifecycle="completed", workstream_lifecycle="completed")
        self.seed_notification("worker_startup_failed")

        payload = workbench.list_work_items(self.db_path, bucket="history")

        self.assertEqual(payload["items"][0]["bucket"], "history")
        self.assertEqual(payload["counts"]["needs_attention"], 0)

        active_payload = workbench.list_work_items(self.db_path)
        self.assertEqual(active_payload["items"], [])
        self.assertEqual(active_payload["counts"]["history"], 1)

    def test_closed_completed_source_keeps_old_failures_in_history(self):
        from robert_agent.readmodels import workbench

        self.seed_workstream(task_lifecycle="failed", workstream_lifecycle="completed")
        self.seed_publish_action(publish_status="skipped")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE github_sources SET state='closed'")
            conn.execute("UPDATE attempts SET status='failed'")
        payload = workbench.list_work_items(self.db_path, bucket="history")
        self.assertEqual(payload["counts"]["needs_attention"], 0)
        self.assertEqual(payload["items"][0]["bucket"], "history")

        # A closed source must not hide work that is still running or publishing.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status='running'")
        self.assertEqual(workbench.list_work_items(self.db_path)["counts"]["history"], 0)

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status='failed'")
            conn.execute("UPDATE github_actions SET publish_status='not_published'")
        self.assertEqual(workbench.list_work_items(self.db_path)["counts"]["history"], 0)

    def test_legacy_read_only_detail_without_board_table_remains_available(self):
        from robert_agent.readmodels import workbench

        workstream_id, _task_id = self.seed_workstream()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DROP TABLE work_items")
        detail = workbench.get_work_item(self.db_path, workstream_id)
        self.assertEqual(detail["id"], workstream_id)
        self.assertIsNone(detail["work_item_id"])

        self.assertEqual(len(workbench.list_work_items(self.db_path)["items"]), 1)

    def test_exhausted_remediation_episode_is_operator_attention(self):
        from robert_agent.readmodels import workbench

        self.seed_workstream(
            task_lifecycle="completed",
            workstream_lifecycle="completed",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status,
                  first_seen_at, updated_at, terminal_at, metadata_json
                )
                VALUES (
                  'episode-ci', 'workstream-1', 'ci', 'head-1:base-1',
                  'head-1', 'base-1', 'exhausted',
                  '2026-07-26T00:00:00Z', '2026-07-26T00:01:00Z',
                  '2026-07-26T00:01:00Z',
                  '{"last_transition_reason":"failure_evidence_unavailable"}'
                )
                """
            )

        payload = workbench.list_work_items(self.db_path)

        self.assertEqual(payload["items"][0]["bucket"], "needs_attention")
        self.assertEqual(
            payload["items"][0]["reason_code"],
            "pr_remediation_exhausted",
        )
        self.assertEqual(
            payload["items"][0]["signals"]["remediation_status"],
            "exhausted",
        )

    def test_newer_resolved_episode_clears_old_exhausted_attention(self):
        from robert_agent.readmodels import workbench

        self.seed_workstream(
            task_lifecycle="completed",
            workstream_lifecycle="completed",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO pr_remediation_episodes(
                  episode_id, workstream_id, episode_kind, subject_key,
                  observed_head_sha, observed_base_sha, status,
                  first_seen_at, updated_at, terminal_at, metadata_json
                )
                VALUES (?, 'workstream-1', 'ci', ?, ?, 'base-1', ?,
                        ?, ?, ?, ?)
                """,
                [
                    (
                        "episode-old",
                        "head-1:base-1",
                        "head-1",
                        "exhausted",
                        "2026-07-26T00:00:00Z",
                        "2026-07-26T00:01:00Z",
                        "2026-07-26T00:01:00Z",
                        '{"last_transition_reason":"max_attempts"}',
                    ),
                    (
                        "episode-new",
                        "head-2:base-1",
                        "head-2",
                        "resolved",
                        "2026-07-26T00:02:00Z",
                        "2026-07-26T00:03:00Z",
                        "2026-07-26T00:03:00Z",
                        "{}",
                    ),
                ],
            )

        payload = workbench.list_work_items(
            self.db_path,
            bucket="history",
        )

        self.assertEqual(payload["items"][0]["bucket"], "history")
        self.assertEqual(payload["items"][0]["reason_code"], "inactive")

    def test_search_qualifiers_and_cursor_cover_the_full_database(self):
        from robert_agent.readmodels import workbench

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
        self.assertTrue({item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"]))

    def test_detail_returns_structured_safe_data(self):
        from robert_agent.readmodels import workbench

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

    def test_failed_open_github_detail_exposes_retrigger_control(self):
        from robert_agent.readmodels import workbench

        workstream_id, task_id = self.seed_workstream(
            task_lifecycle="failed",
            workstream_lifecycle="failed",
        )

        detail = workbench.get_work_item(self.db_path, workstream_id)

        self.assertEqual(
            detail["controls"],
            {
                "can_retrigger": True,
                "retrigger_pending": False,
                "task_id": task_id,
            },
        )

    def test_web_routes_workbench_list_and_detail(self):
        from robert_agent.interfaces.web import server as web

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

    def test_web_routes_encoded_github_workstream_id_with_repo_slash(self):
        from robert_agent.interfaces.web import server as web

        workstream_id, _task_id = self.seed_workstream()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE workstreams SET workstream_id = 'github:Org/repo!1' WHERE workstream_id = ?",
                (workstream_id,),
            )
            conn.execute(
                "UPDATE tasks SET workstream_id = 'github:Org/repo!1' WHERE workstream_id = ?",
                (workstream_id,),
            )
            conn.execute(
                "UPDATE workstream_sources SET workstream_id = 'github:Org/repo!1' WHERE workstream_id = ?",
                (workstream_id,),
            )

        status, _headers, body = web.build_http_response(
            "/api/work-items/github%3AOrg%2Frepo%211",
            self.db_path,
        )

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["id"], "github:Org/repo!1")

    def test_web_returns_safe_workbench_errors(self):
        from robert_agent.interfaces.web import server as web

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
        from robert_agent.interfaces.web import server as web

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

    def test_cursor_survives_anchor_update_for_every_sort(self):
        from robert_agent.readmodels import workbench

        for number in range(1, 5):
            self.seed_workstream(str(number))
        for sort in ("priority", "updated-asc", "updated-desc"):
            with self.subTest(sort=sort):
                first = workbench.list_work_items(self.db_path, sort=sort, limit=2)
                anchor = first["items"][-1]
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE tasks SET updated_at='2027-01-01T00:00:00+00:00' WHERE task_id=?",
                        (anchor["agent"]["task_id"],),
                    )
                second = workbench.list_work_items(self.db_path, sort=sort, limit=2, cursor=first["next_cursor"])
                self.assertTrue(second["items"])
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute(
                        "UPDATE tasks SET updated_at=? WHERE task_id=?",
                        (anchor["updated_at"], anchor["agent"]["task_id"]),
                    )

    def test_preview_availability_matches_contained_file_policy(self):
        from robert_agent.readmodels import workbench

        stream, task = self.seed_workstream()
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.txt"
            outside.write_text("private")
            link = self.root / "linked.txt"
            link.symlink_to(outside)
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO artifacts(artifact_id,task_id,artifact_type,path,created_at) VALUES('linked',?,'worker_stdout',?,'2026-11-08')",
                    (task, str(link)),
                )
            detail = workbench.get_work_item(self.db_path, stream)
            self.assertFalse(detail["artifacts"][0]["preview_available"])

    def test_list_reads_latest_execution_evidence_without_artifact_history(self):
        from unittest.mock import patch
        from robert_agent.readmodels import workbench
        from robert_agent.persistence.queries.readviews import task_history

        stream, task = self.seed_workstream()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for number in range(2, 52):
                attempt = f"attempt-{number}"
                conn.execute(
                    "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES(?,?,?,'completed')",
                    (attempt, task, number),
                )
                conn.execute(
                    "INSERT INTO worker_phases(phase_id,attempt_id,phase,status,created_at,summary) VALUES(?,?,'verify','completed','2026-11-08','checked')",
                    (attempt, attempt),
                )
                conn.execute(
                    "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at) VALUES(?,?,?,'comment','2026-11-08')",
                    (attempt, task, attempt),
                )
        with patch.object(task_history, "artifacts_for_tasks", wraps=task_history.artifacts_for_tasks) as artifacts:
            page = workbench.list_work_items(self.db_path, limit=1)
        self.assertEqual(page["items"][0]["agent"]["attempt_no"], 51)
        self.assertEqual(page["items"][0]["agent"]["phase"], "verify")
        artifacts.assert_not_called()
        detail = workbench.get_work_item(self.db_path, stream)
        self.assertEqual(len(detail["attempts"]), 51)


if __name__ == "__main__":
    unittest.main()

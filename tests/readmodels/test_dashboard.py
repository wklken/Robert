from robert_agent.readmodels import dashboard
from contextlib import closing
import json
import sqlite3
from tests.support.dashboard import DashboardFixture


class DashboardTests(DashboardFixture):
    def test_global_decision_alert_warning_becomes_resolved_history(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO notifications(notification_id,notification_type,status,created_at,metadata_json) "
                "VALUES ('decision-alert','supervision_decision_unavailable','recorded','2030-01-01T00:00:00Z',?)",
                (json.dumps({"summary": "Decision service unavailable", "safe_error": "gateway error"}),),
            )
        for status, severity in (("recorded", "warning"), ("resolved", "ok")):
            with self.subTest(status=status):
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.execute("UPDATE notifications SET status=? WHERE notification_id='decision-alert'", (status,))
                items = dashboard.build_dashboard_data(db_path)["notification_center"]
                notice = next(item for item in items if item["id"] == "notification:decision-alert")
                self.assertEqual(notice["severity"], severity)
                self.assertIsNone(notice["task_id"])

    def test_web_builds_dashboard_data(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(data["summary"]["tasks"], 2)
        self.assertEqual(
            data["repos"],
            [
                {
                    "repo_id": "repo-1",
                    "full_name": "example/backend",
                }
            ],
        )
        self.assertEqual(data["recent_tasks"][0]["task_id"], "task-2")
        self.assertIn("work_items", data)
        task_by_id = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(task_by_id["task-1"]["repo_id"], "repo-1")
        self.assertEqual(task_by_id["task-1"]["repo_full_name"], "example/backend")
        self.assertEqual(task_by_id["task-1"]["source_title"], "Question")
        self.assertEqual(data["recent_events"][0]["source_title"], "Question")
        self.assertEqual(data["wakeup_summary"]["pending"], 1)
        self.assertEqual(data["recent_wakeups"][0]["result_id"], "result-1")
        self.assertIn("acceptance_metrics", data)

    def test_web_groups_work_by_github_source(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-2', 'event-2', 'trigger', '2030-01-01T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                UPDATE tasks
                SET updated_at = '2030-01-01T00:00:00+00:00'
                WHERE task_id = 'task-2'
                """
            )

        data = dashboard.build_dashboard_data(db_path)
        work_items = {item["source_key"]: item for item in data["work_items"]}
        item = work_items["github:example/backend#1"]

        self.assertEqual(item["task_count"], 2)
        self.assertEqual(item["repo_id"], "repo-1")
        self.assertEqual(item["repo_full_name"], "example/backend")
        self.assertEqual(item["source_title"], "Question")
        self.assertEqual(item["tasks"][0]["task_id"], "task-2")
        self.assertEqual(item["latest_task_updated_at"], "2030-01-01T00:00:00+00:00")

    def test_web_dashboard_data_lists_all_repositories(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo-2', 'Org/second-repo', 'robert-bot', 'main', '/repo2', '/repo2/.worktrees')
                """
            )

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(
            data["repos"],
            [
                {"repo_id": "repo-2", "full_name": "Org/second-repo"},
                {
                    "repo_id": "repo-1",
                    "full_name": "example/backend",
                },
            ],
        )

    def test_web_builds_notification_center(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertIn("notification_center", data)
        self.assertTrue(any(item["kind"] == "notification" for item in data["notification_center"]))
        self.assertTrue(
            {
                "id",
                "kind",
                "severity",
                "title",
                "summary",
                "created_at",
                "metadata",
            }
            <= set(data["notification_center"][0])
        )

    def test_web_sorts_notifications_by_newest_time(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO notifications(
                  notification_id, task_id, notification_type, channel, status,
                  created_at, metadata_json
                )
                VALUES (
                  'notification-2', 'task-1', 'worker_stale',
                  'local', 'recorded', '2030-01-01T00:00:00+00:00', '{"summary": "latest"}'
                )
                """
            )

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(data["notification_center"][0]["id"], "notification:notification-2")

    def test_web_exposes_knowledge_review_data(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_knowledge_candidate(db_path)

        data = dashboard.build_dashboard_data(db_path)

        review = data["knowledge_review"]
        self.assertEqual(review["counts"]["pending"], 1)
        self.assertEqual(review["counts"]["active"], 0)
        self.assertEqual(review["pending_candidates"][0]["candidate_id"], "kc-1")
        self.assertEqual(review["pending_candidates"][0]["title"], "Prefer update-existing-pr")
        self.assertEqual(review["submission_boundary"], "local_runtime_knowledge_only")

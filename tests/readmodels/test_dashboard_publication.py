from robert_agent.readmodels import dashboard
from tests.support.dashboard import DashboardFixture


class DashboardPublicationTests(DashboardFixture):
    def test_web_marks_publish_failures_as_attention_steps_with_command(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_publish_failure(db_path)

        data = dashboard.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "needs_attention")
        self.assertEqual(task["next_action"], "inspect_github_publish_failure")
        steps = {(step["target_kind"], step["target_id"]): step for step in data["operator_next_steps"]}
        self.assertEqual(
            steps[("task", "task-1")],
            {
                "target_kind": "task",
                "target_id": "task-1",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "workstream_id": "ws-1",
                "route_id": "comment-analysis",
                "severity": "critical",
                "next_action": "inspect_github_publish_failure",
                "reason": "needs_attention",
                "notification_type": "github_publish_failed",
                "safe_error": "gh: authentication failed",
                "command": [
                    "gh",
                    "api",
                    "repos/x/y/issues/1/comments",
                    "-f",
                    "body=ready",
                ],
            },
        )

    def test_web_publish_failure_step_ignores_unrelated_latest_notification(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_publish_failure(db_path)
        self._record_unrelated_latest_notification(db_path)

        data = dashboard.build_dashboard_data(db_path)

        steps = {(step["target_kind"], step["target_id"]): step for step in data["operator_next_steps"]}
        self.assertEqual(steps[("task", "task-1")]["notification_type"], "github_publish_failed")
        self.assertEqual(steps[("task", "task-1")]["safe_error"], "gh: authentication failed")
        self.assertEqual(
            steps[("task", "task-1")]["command"],
            ["gh", "api", "repos/x/y/issues/1/comments", "-f", "body=ready"],
        )

    def test_web_marks_skipped_publish_actions_as_attention(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_skipped_publish_action(db_path)

        data = dashboard.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "needs_attention")
        self.assertEqual(task["next_action"], "inspect_github_publish_skipped")
        steps = {(step["target_kind"], step["target_id"]): step for step in data["operator_next_steps"]}
        self.assertEqual(steps[("task", "task-1")]["notification_type"], "github_publish_skipped")
        self.assertEqual(
            steps[("task", "task-1")]["safe_error"],
            "publisher does not support action_type close_issue",
        )

    def test_web_waiting_for_user_with_unpublished_question_waits_for_publish(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._mark_summary_task_waiting_for_user(db_path)

        data = dashboard.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "waiting_publish")
        self.assertEqual(task["next_action"], "publish_accepted_github_actions")

    def test_web_does_not_report_recovered_publish_failure_as_attention(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_recovered_publish_failure(db_path)

        data = dashboard.build_dashboard_data(db_path)

        task = next(task for task in data["recent_tasks"] if task["task_id"] == "task-1")
        self.assertEqual(task["operator_state"], "completed")
        self.assertEqual(task["next_action"], "none")
        self.assertNotIn(
            ("task", "task-1"),
            {(step["target_kind"], step["target_id"]) for step in data["operator_next_steps"]},
        )

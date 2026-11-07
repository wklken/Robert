from robert_agent.readmodels import dashboard
from tests.support.dashboard import DashboardFixture


class DashboardOperatorStateTests(DashboardFixture):
    def test_web_annotates_recent_tasks_with_operator_state(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._insert_operator_state_examples(db_path)

        data = dashboard.build_dashboard_data(db_path)

        tasks = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(tasks["task-worker"]["operator_state"], "worker_running")
        self.assertEqual(tasks["task-worker"]["next_action"], "wait_for_worker_completion")
        self.assertEqual(tasks["task-worker"]["latest_attempt_status"], "running")
        self.assertEqual(tasks["task-stale"]["operator_state"], "worker_stale")
        self.assertEqual(tasks["task-stale"]["next_action"], "inspect_worker_heartbeat")
        self.assertEqual(tasks["task-stale"]["latest_attempt_status"], "stale")
        self.assertEqual(tasks["task-timeout"]["operator_state"], "needs_attention")
        self.assertEqual(tasks["task-timeout"]["next_action"], "inspect_worker_timeout")
        self.assertEqual(tasks["task-timeout"]["latest_attempt_status"], "failed")
        self.assertEqual(tasks["task-publish"]["operator_state"], "waiting_publish")
        self.assertEqual(tasks["task-publish"]["next_action"], "publish_accepted_github_actions")
        self.assertEqual(tasks["task-publish"]["pending_publish_actions"], 1)
        self.assertEqual(tasks["task-user"]["operator_state"], "waiting_user")
        self.assertEqual(tasks["task-user"]["next_action"], "wait_for_trusted_user_reply")
        self.assertEqual(tasks["task-failed"]["operator_state"], "needs_attention")
        self.assertEqual(tasks["task-failed"]["next_action"], "inspect_failure_notification")
        self.assertEqual(
            tasks["task-failed"]["latest_notification"]["metadata"]["audit"]["safe_error"],
            "open_pr action missing fields: ['base']",
        )

    def test_web_flags_repeated_worker_resume_notifications(self):
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)
        self._record_repeated_worker_resume_notifications(db_path)

        data = dashboard.build_dashboard_data(db_path)

        tasks = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(tasks["task-worker"]["operator_state"], "worker_retrying")
        self.assertEqual(tasks["task-worker"]["next_action"], "inspect_worker_resume")
        self.assertEqual(tasks["task-worker"]["worker_resume_count"], 2)
        self.assertEqual(data["operator_state_counts"]["worker_retrying"], 1)
        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(alerts["worker_retrying_tasks"]["severity"], "warning")

    def test_web_flags_worker_result_pending_audit(self):
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)
        self._record_result_pending_audit_task(db_path)

        data = dashboard.build_dashboard_data(db_path)

        tasks = {task["task_id"]: task for task in data["recent_tasks"]}
        self.assertEqual(tasks["task-result-pending"]["operator_state"], "result_pending_audit")
        self.assertEqual(tasks["task-result-pending"]["next_action"], "wait_next_cycle_or_run_once")
        self.assertEqual(tasks["task-result-pending"]["latest_attempt_status"], "completed")
        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(alerts["result_pending_audit_tasks"]["severity"], "warning")

    def test_web_counts_operator_states_for_dashboard_overview(self):
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(
            data["operator_state_counts"],
            {
                "needs_attention": 2,
                "waiting_publish": 1,
                "waiting_user": 1,
                "worker_running": 1,
                "worker_stale": 1,
            },
        )

    def test_web_worker_next_steps_include_attempt_evidence(self):
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("task", "task-stale")]["next_action"], "inspect_worker_heartbeat")
        self.assertEqual(steps[("task", "task-stale")]["attempt_id"], "attempt-stale")
        self.assertEqual(steps[("task", "task-stale")]["attempt_status"], "stale")
        self.assertIsNotNone(steps[("task", "task-stale")]["heartbeat_at"])
        self.assertIsNotNone(steps[("task", "task-stale")]["started_at"])
        self.assertEqual(steps[("task", "task-timeout")]["next_action"], "inspect_worker_timeout")
        self.assertEqual(steps[("task", "task-timeout")]["attempt_id"], "attempt-timeout")
        self.assertEqual(steps[("task", "task-timeout")]["attempt_status"], "failed")
        self.assertIsNotNone(steps[("task", "task-timeout")]["finished_at"])

    def test_web_failure_next_steps_include_notification_evidence(self):
        db_path = self.root / "dd.sqlite3"
        self._init_operator_overview_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        steps = {
            (step["target_kind"], step["target_id"]): step
            for step in data["operator_next_steps"]
        }
        self.assertEqual(steps[("task", "task-failed")]["next_action"], "inspect_failure_notification")
        self.assertEqual(steps[("task", "task-failed")]["notification_type"], "worker_result_rejected")
        self.assertEqual(steps[("task", "task-failed")]["result_id"], "result-failed")
        self.assertEqual(steps[("task", "task-failed")]["safe_error"], "open_pr action missing fields: ['base']")

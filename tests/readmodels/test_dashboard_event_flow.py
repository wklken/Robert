from robert_agent.readmodels import dashboard
from tests.support.dashboard import DashboardFixture


class DashboardEventFlowTests(DashboardFixture):
    def test_web_exposes_task_route_decision_evidence(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        task = {task["task_id"]: task for task in data["recent_tasks"]}["task-2"]
        self.assertEqual(task["route_decision_id"], "route-2")
        self.assertEqual(task["route_confidence"], "high")
        self.assertEqual(task["allowed_github_actions"], ["push_existing_pr", "open_pr", "comment"])
        self.assertEqual(
            task["recommended_skills"],
            [
                "fast-small-pr",
                "fast-code-path",
                "fast-add-tests",
                "fast-test-fix",
                "fast-preflight",
            ],
        )

    def test_web_exposes_recent_event_flow_evidence(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        event = {event["event_fingerprint"]: event for event in data["recent_events"]}["comment:1"]
        self.assertEqual(event["event_fingerprint"], "comment:1")
        self.assertEqual(event["source_key"], "github:example/backend#1")
        self.assertEqual(event["source_type"], "issue")
        self.assertEqual(event["number"], 1)
        self.assertEqual(event["authorization_status"], "authorized")
        self.assertEqual(event["task_id"], "task-1")
        self.assertEqual(event["task_relationship"], "trigger")
        self.assertEqual(event["workstream_id"], "ws-1")
        self.assertEqual(event["payload"]["intent"], "analysis")

    def test_web_counts_event_flow_risk_states(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(
            data["event_flow_counts"],
            {
                "authorization_status": {
                    "authorized": 2,
                    "pending_actor_permission": 1,
                },
                "task_relationship": {
                    "trigger": 1,
                    "unattached": 2,
                },
                "authorized_unattached": 1,
                "ignored_events": 0,
            },
        )

    def test_web_counts_ignored_events_for_operator_context(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_ignored_untrusted_event(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(data["event_flow_counts"]["ignored_events"], 1)

    def test_web_counts_real_authorized_trigger_unattached_events(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_authorized_trigger_unattached_event(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(data["event_flow_counts"]["authorized_unattached"], 2)
        steps = {(step["target_kind"], step["target_id"]): step for step in data["operator_next_steps"]}
        self.assertEqual(
            steps[("event", "event-authorized-trigger")]["next_action"],
            "inspect_recent_events",
        )
        self.assertEqual(
            steps[("event", "event-authorized-trigger")]["reason"],
            "authorized_unattached",
        )

    def test_web_surfaces_pending_authorization_events(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_pending_authorization_event(db_path)

        data = dashboard.build_dashboard_data(db_path)

        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(
            alerts["pending_authorization_events"],
            {
                "alert_type": "pending_authorization_events",
                "count": 1,
                "severity": "warning",
                "next_action": "resolve_authorization_lookup",
            },
        )
        steps = {(step["target_kind"], step["target_id"]): step for step in data["operator_next_steps"]}
        self.assertEqual(
            steps[("event", "event-pending-authorization")]["next_action"],
            "resolve_authorization_lookup",
        )
        self.assertEqual(
            steps[("event", "event-pending-authorization")]["reason"],
            "pending_authorization",
        )

    def test_web_builds_operator_alerts_from_risk_counts(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(
            alerts["authorized_unattached_events"],
            {
                "alert_type": "authorized_unattached_events",
                "count": 1,
                "severity": "warning",
                "next_action": "inspect_recent_events",
            },
        )
        self.assertEqual(
            alerts["pending_actor_permission_events"],
            {
                "alert_type": "pending_actor_permission_events",
                "count": 1,
                "severity": "warning",
                "next_action": "resolve_actor_permission",
            },
        )
        self.assertEqual(
            alerts["waiting_publish_tasks"],
            {
                "alert_type": "waiting_publish_tasks",
                "count": 1,
                "severity": "info",
                "next_action": "publish_accepted_github_actions",
            },
        )

    def test_web_operator_alerts_include_pending_events_outside_recent_window(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_pending_authorization_event(db_path)
        self._record_ignored_untrusted_event(db_path)

        data = dashboard.build_dashboard_data(db_path, history_limit=1)

        self.assertNotEqual(
            data["recent_events"][0]["event_id"],
            "event-pending-authorization",
        )
        alerts = {alert["alert_type"]: alert for alert in data["operator_alerts"]}
        self.assertEqual(alerts["pending_actor_permission_events"]["count"], 1)
        self.assertEqual(alerts["pending_authorization_events"]["count"], 1)

    def test_web_operator_next_steps_include_pending_events_outside_recent_window(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_pending_authorization_event(db_path)
        self._record_ignored_untrusted_event(db_path)

        data = dashboard.build_dashboard_data(db_path, history_limit=1)

        steps = {(step["target_kind"], step["target_id"]): step for step in data["operator_next_steps"]}
        self.assertEqual(
            steps[("event", "event-3")]["next_action"],
            "resolve_actor_permission",
        )
        self.assertEqual(
            steps[("event", "event-pending-authorization")]["next_action"],
            "resolve_authorization_lookup",
        )

    def test_web_builds_operator_next_steps_with_target_ids(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

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
                "severity": "info",
                "next_action": "publish_accepted_github_actions",
                "reason": "waiting_publish",
                "action_id": "action-1",
                "action_type": "comment",
                "target_url": "https://github.com/x/y/issues/1",
            },
        )
        self.assertEqual(
            steps[("event", "event-2")],
            {
                "target_kind": "event",
                "target_id": "event-2",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "event_fingerprint": "comment:2",
                "actor_login": "wklken",
                "severity": "warning",
                "next_action": "inspect_recent_events",
                "reason": "authorized_unattached",
            },
        )
        self.assertEqual(
            steps[("event", "event-3")],
            {
                "target_kind": "event",
                "target_id": "event-3",
                "github_url": "https://github.com/x/y/issues/1",
                "source_key": "github:example/backend#1",
                "source_title": "Question",
                "event_fingerprint": "comment:3",
                "actor_login": "external-user",
                "severity": "warning",
                "next_action": "resolve_actor_permission",
                "reason": "pending_actor_permission",
            },
        )

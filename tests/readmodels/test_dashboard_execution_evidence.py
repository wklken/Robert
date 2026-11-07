from robert_agent.readmodels import dashboard
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from tests.support.dashboard import DashboardFixture


class DashboardExecutionEvidenceTests(DashboardFixture):
    def test_web_exposes_recent_task_artifacts_for_debugging(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        task = next(
            task for task in data["recent_tasks"] if task["task_id"] == "task-1"
        )
        artifact_dir = self.root / "task-artifacts"
        prompt_path = artifact_dir / "prompt.md"
        stdout_path = artifact_dir / "worker.stdout.log"
        stderr_path = artifact_dir / "worker.stderr.log"
        self.assertEqual(
            task["artifacts"],
            {
                "prompt": {
                    "path": str(prompt_path),
                    "bytes": prompt_path.stat().st_size,
                },
                "worker_stderr": {
                    "path": str(stderr_path),
                    "bytes": stderr_path.stat().st_size,
                },
                "worker_stdout": {
                    "path": str(stdout_path),
                    "bytes": stdout_path.stat().st_size,
                },
            },
        )

    def test_web_exposes_recent_run_steps_for_debugging(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(data["recent_runs"][0]["run_id"], "run-1")
        self.assertEqual(data["recent_runs"][0]["status"], "failed")
        steps = {
            step["step_key"]: step["status"] for step in data["recent_runs"][0]["steps"]
        }
        self.assertEqual(steps["validate_config"], "succeeded")
        self.assertEqual(steps["dispatch"], "failed")
        self.assertEqual(steps["summarize"], "skipped")

    def test_web_exposes_recent_run_repo_steps(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO run_repo_steps(
                  step_id, run_id, repo_id, step_key, status,
                  started_at, finished_at, output_json, error_json
                )
                VALUES ('repo-step-1', 'run-1', 'repo-1', 'discover', 'failed', ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    json.dumps({"raw_event_count": 0}, sort_keys=True),
                    json.dumps({"status": "api_failed"}, sort_keys=True),
                ),
            )

        data = dashboard.build_dashboard_data(db_path)
        repo_steps = data["recent_runs"][0]["repo_steps"]

        self.assertEqual(repo_steps[0]["repo_id"], "repo-1")
        self.assertEqual(repo_steps[0]["repo_full_name"], "example/backend")
        self.assertEqual(repo_steps[0]["output"]["raw_event_count"], 0)
        self.assertEqual(repo_steps[0]["error"]["status"], "api_failed")

    def test_web_orders_recent_run_steps_by_runtime_flow(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES (?, 'run-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("step-audit", "audit_results", now, now),
                    ("step-publish", "publish_actions", now, now),
                    ("step-discover", "discover", now, now),
                ],
            )

        data = dashboard.build_dashboard_data(db_path)
        step_keys = [step["step_key"] for step in data["recent_runs"][0]["steps"]]

        self.assertLess(
            step_keys.index("audit_results"), step_keys.index("publish_actions")
        )
        self.assertLess(step_keys.index("publish_actions"), step_keys.index("discover"))

    def test_web_orders_multi_repo_run_steps_by_runtime_flow(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO run_steps(step_id, run_id, step_key, status, started_at, finished_at)
                VALUES (?, 'run-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("step-acquire-lease", "acquire_lease", now, now),
                    ("step-publish", "publish_actions", now, now),
                    ("step-reconcile", "reconcile", now, now),
                    ("step-discover", "discover", now, now),
                    ("step-route", "route", now, now),
                    ("step-repo-pipelines", "repo_pipelines", now, now),
                    ("step-discover-notifications", "discover_notifications", now, now),
                ],
            )

        data = dashboard.build_dashboard_data(db_path)
        step_keys = [step["step_key"] for step in data["recent_runs"][0]["steps"]]

        self.assertLess(
            step_keys.index("discover_notifications"), step_keys.index("acquire_lease")
        )
        self.assertLess(
            step_keys.index("publish_actions"), step_keys.index("reconcile")
        )
        self.assertLess(step_keys.index("reconcile"), step_keys.index("discover"))
        self.assertLess(step_keys.index("route"), step_keys.index("repo_pipelines"))
        self.assertLess(step_keys.index("repo_pipelines"), step_keys.index("dispatch"))

    def test_web_orders_repo_steps_by_pipeline_flow(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executemany(
                """
                INSERT INTO run_repo_steps(
                  step_id, run_id, repo_id, step_key, status, started_at, finished_at
                )
                VALUES (?, 'run-1', 'repo-1', ?, 'succeeded', ?, ?)
                """,
                [
                    ("repo-step-route", "route", now, now),
                    ("repo-step-acquire", "acquire_lease", now, now),
                    ("repo-step-reconcile", "reconcile", now, now),
                    ("repo-step-supervise", "supervise", now, now),
                ],
            )

        data = dashboard.build_dashboard_data(db_path)
        step_keys = [step["step_key"] for step in data["recent_runs"][0]["repo_steps"]]

        self.assertEqual(
            step_keys, ["acquire_lease", "supervise", "reconcile", "route"]
        )

    def test_web_exposes_recent_github_action_audit_and_publish_status(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        data = dashboard.build_dashboard_data(db_path)

        self.assertEqual(data["recent_actions"][0]["action_id"], "action-1")
        self.assertEqual(data["recent_actions"][0]["audit_status"], "accepted")
        self.assertEqual(data["recent_actions"][0]["publish_status"], "not_published")

    def test_web_exposes_recent_notifications_for_rejected_worker_results(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_rejected_result(db_path)

        data = dashboard.build_dashboard_data(db_path)

        notification = data["recent_notifications"][0]
        self.assertEqual(notification["notification_type"], "worker_result_rejected")
        self.assertEqual(notification["task_id"], "task-1")
        self.assertEqual(notification["metadata"]["result_id"], "result-1")
        self.assertEqual(
            notification["metadata"]["audit"]["safe_error"],
            "open_pr action missing fields: ['base']",
        )

    def test_web_exposes_worker_result_skill_evidence(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_worker_result_evidence(db_path)

        data = dashboard.build_dashboard_data(db_path)

        result = data["recent_worker_results"][0]
        self.assertEqual(result["result_id"], "result-1")
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["consumed_event_fingerprints"], ["comment:1"])
        self.assertEqual(
            result["verification"],
            [{"command": "python -m unittest", "status": "passed"}],
        )
        self.assertEqual(result["metadata"]["used_skills"], ["fast-code-path"])
        self.assertEqual(result["metadata"]["audit"]["status"], "accepted")
        self.assertEqual(data["used_skill_counts"], {"fast-code-path": 1})

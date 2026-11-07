from robert_agent.readmodels import dashboard
from contextlib import closing
import json
import os
import sqlite3
from datetime import datetime, timezone
from tests.support.summary_database import SummaryFixture


class StatusTests(SummaryFixture):
    def test_status_surfaces_daemon_summary(self):
        from robert_agent.readmodels import chat as chat_status
        from robert_agent.readmodels import status
        from robert_agent.execution import runtime_state as daemon_state
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "daemon-status.sqlite3"
        storage.init_database(db_path)
        with closing(daemon_state.connect(db_path)) as conn, conn:
            run = daemon_state.start_daemon_run(
                conn,
                config_path="/tmp/config.yml",
                owner_id="daemon-run-1",
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.acquire_daemon_lease(
                conn,
                resource_key="daemon:main",
                owner_id="daemon-run-1",
                ttl_seconds=60,
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "run_once_triggered",
                "running",
                {"reason": "event_queue"},
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "live_poll_skipped_rate_limit",
                "skipped",
                {"reason": "core_rate_limit_floor"},
                now="2026-07-03T00:00:01+00:00",
            )

        dashboard_data = dashboard.build_dashboard_data(db_path)
        status = status.build_status(db_path)
        message = chat_status.format_status(dashboard_data)

        self.assertEqual(
            dashboard_data["daemon"]["latest_event"]["event_type"],
            "live_poll_skipped_rate_limit",
        )
        self.assertEqual(dashboard_data["daemon"]["latest_lease"]["status"], "active")
        self.assertEqual(dashboard_data["daemon"]["recent_events"][0]["event_type"], "live_poll_skipped_rate_limit")
        self.assertEqual(dashboard_data["daemon"]["recent_events"][1]["event_type"], "run_once_triggered")
        self.assertEqual(status["daemon"]["latest_run"]["status"], "running")
        self.assertIn("Daemon:", message)
        self.assertIn("live_poll_skipped_rate_limit", message)

    def test_status_reports_compact_overview_json(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_status("--db", str(db_path), "status")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["format"], "json")
        self.assertTrue(payload["checked_at_utc"].endswith("Z"))
        self.assertEqual(payload["data"]["summary"]["tasks"], 2)
        self.assertEqual(payload["data"]["wakeup_summary"]["pending"], 1)
        self.assertIn("acceptance_metrics", payload["data"])
        self.assertEqual(payload["data"]["latest_run"]["run_id"], "run-1")
        self.assertEqual(payload["data"]["recent_tasks"][0]["task_id"], "task-2")

    def test_status_reports_task_by_id_without_history_markdown(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_status("--db", str(db_path), "task", "task-1")

        self.assertEqual(code, 0)
        data = payload["data"]
        self.assertEqual(data["task_id"], "task-1")
        self.assertEqual(data["workstream_id"], "ws-1")
        self.assertEqual(data["latest_attempt"]["attempt_id"], "attempt-1")
        self.assertEqual(data["latest_phase"]["phase"], "analyze")
        self.assertEqual(data["latest_result"]["result_id"], "result-1")
        self.assertEqual(data["results"][0]["result_id"], "result-1")
        self.assertEqual(data["results"][0]["handoff"], "ready")
        self.assertEqual(data["action_counts"]["total"], 1)
        self.assertEqual(data["latest_notification"]["notification_id"], "notification-1")
        self.assertEqual(data["actions"][0]["action_id"], "action-1")
        self.assertIn("prompt", {artifact["artifact_type"] for artifact in data["artifacts"]})
        self.assertEqual(data["events"][0]["event_fingerprint"], "comment:1")

    def test_status_reports_run_attempt_and_workstream_by_id(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        run_code, run_payload = self._run_status("--db", str(db_path), "run", "run-1")
        attempt_code, attempt_payload = self._run_status("--db", str(db_path), "attempt", "attempt-1")
        stream_code, stream_payload = self._run_status("--db", str(db_path), "workstream", "ws-1")

        self.assertEqual(run_code, 0)
        self.assertEqual(run_payload["data"]["steps"][1]["step_key"], "dispatch")
        self.assertEqual(attempt_code, 0)
        self.assertEqual(attempt_payload["data"]["task_id"], "task-1")
        self.assertEqual(attempt_payload["data"]["dispatch"]["command"]["argv0"], "cbc")
        self.assertEqual(attempt_payload["data"]["dispatch"]["command"]["argc"], 4)
        self.assertNotIn("very long prompt", json.dumps(attempt_payload["data"]["dispatch"]))
        self.assertEqual(attempt_payload["data"]["process"]["pid"], os.getpid())
        self.assertEqual(attempt_payload["data"]["process"]["status"], "running")
        self.assertEqual(attempt_payload["data"]["phases"][0]["phase"], "analyze")
        self.assertEqual(stream_code, 0)
        self.assertEqual(stream_payload["data"]["workstream_id"], "ws-1")
        self.assertEqual(stream_payload["data"]["tasks"][0]["task_id"], "task-2")

    def test_status_reports_latest_recent_event_and_artifact(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        latest_code, latest_payload = self._run_status("--db", str(db_path), "run", "latest")
        runs_code, runs_payload = self._run_status("--db", str(db_path), "runs", "--limit", "3")
        event_code, event_payload = self._run_status("--db", str(db_path), "event", "1")
        events_code, events_payload = self._run_status("--db", str(db_path), "events", "analysis", "--limit", "5")
        source_code, source_payload = self._run_status("--db", str(db_path), "source", "1")
        artifact_code, artifact_payload = self._run_status(
            "--db",
            str(db_path),
            "artifact",
            "task-1",
            "worker_stderr",
            "--max-bytes",
            "64",
        )

        self.assertEqual(latest_code, 0)
        self.assertEqual(latest_payload["data"]["run_id"], "run-1")
        self.assertEqual(runs_code, 0)
        self.assertEqual(runs_payload["data"][0]["run_id"], "run-1")
        self.assertEqual(runs_payload["data"][0]["step_status_counts"]["failed"], 1)
        self.assertEqual(event_code, 0)
        self.assertEqual(event_payload["data"]["event_fingerprint"], "comment:1")
        self.assertEqual(event_payload["data"]["tasks"][0]["task_id"], "task-1")
        self.assertEqual(event_payload["data"]["tasks"][0]["workstream_id"], "ws-1")
        self.assertIsNone(event_payload["data"]["tasks"][0]["parent_task_id"])
        self.assertEqual(event_payload["data"]["tasks"][0]["worktree_path"], "/repo/.worktrees/task-1")
        self.assertEqual(event_payload["data"]["tasks"][0]["branch_name"], "codex/dd-1-task")
        self.assertEqual(events_code, 0)
        self.assertEqual(events_payload["data"][0]["event_fingerprint"], "comment:1")
        self.assertEqual(events_payload["data"][0]["linked_task_count"], 1)
        self.assertEqual(source_code, 0)
        self.assertEqual(source_payload["data"]["sources"][0]["source_key"], "github:example/backend#1")
        self.assertEqual(source_payload["data"]["workstreams"][0]["workstream_id"], "ws-1")
        self.assertEqual(source_payload["data"]["tasks"][0]["task_id"], "task-1")
        self.assertEqual(source_payload["data"]["tasks"][0]["worktree_path"], "/repo/.worktrees/task-1")
        self.assertEqual(artifact_code, 0)
        self.assertIn("stderr tail", artifact_payload["data"]["content"])
        self.assertEqual(artifact_payload["data"]["artifact_type"], "worker_stderr")

    def test_status_reports_current_state_closed_source(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        payload = {
            "status": "current_state_closed",
            "source_key": "github:example/backend#1",
            "source_type": "issue",
            "number": 1,
            "terminal_reason": "remote_source_closed",
            "skip_reason": "source_already_closed_before_dispatch",
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_sources
                SET state = 'closed', metadata_json = ?
                WHERE source_id = 'source-1'
                """,
                (json.dumps({"current_state_reconciliation": payload}, sort_keys=True),),
            )
            conn.execute(
                """
                INSERT INTO audit_events(
                  audit_id, repo_id, workstream_id, task_id, event_type, created_at, payload_json
                )
                VALUES ('audit-current-state-closed', 'repo-1', NULL, NULL, 'current_state_closed', ?, ?)
                """,
                (datetime.now(timezone.utc).isoformat(), json.dumps(payload, sort_keys=True)),
            )

        status_code, status_payload = self._run_status("--db", str(db_path), "status")
        source_code, source_payload = self._run_status("--db", str(db_path), "source", "1")

        self.assertEqual(status_code, 0)
        self.assertEqual(status_payload["data"]["summary"]["skipped_closed_sources"], 1)
        self.assertEqual(source_code, 0)
        source_metadata = source_payload["data"]["sources"][0]["metadata"]
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["status"],
            "current_state_closed",
        )
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["skip_reason"],
            "source_already_closed_before_dispatch",
        )

    def test_status_reports_missing_ids(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_status("--db", str(db_path), "attempt", "missing-attempt")

        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_found")
        self.assertIn("missing-attempt", payload["safe_error"])

    def test_status_run_includes_repo_steps(self):
        from robert_agent.readmodels import status
        from robert_agent.persistence.schema import database as storage

        data_dir = self.root / "data"
        repo_root = self.root / "repo-a"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        config_path = self.root / "config.yml"
        db_path = data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo:a', 'Org/a', 'robot', 'main', ?, ?)",
                (str(repo_root), str(worktree_root)),
            )
            conn.execute(
                "INSERT INTO agent_runs(run_id, status, started_at, config_path, dry_run, summary_json) VALUES ('run-a', 'failed', ?, ?, 1, ?)",
                (
                    now,
                    str(config_path),
                    json.dumps(
                        {
                            "overall_status": "partial_failure",
                            "repo_summaries": [{"repo_id": "repo:a", "status": "failed"}],
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO run_repo_steps(step_id, run_id, repo_id, step_key, status, started_at, finished_at, output_json) VALUES ('step-a', 'run-a', 'repo:a', 'discover', 'failed', ?, ?, ?)",
                (now, now, json.dumps({"raw_event_count": 0}, sort_keys=True)),
            )

            run = status.build_run(conn, "run-a")

        self.assertEqual(run["summary"]["overall_status"], "partial_failure")
        self.assertEqual(run["repo_steps"][0]["repo_id"], "repo:a")
        self.assertEqual(run["repo_steps"][0]["repo_full_name"], "Org/a")

    def test_status_orders_multi_repo_run_steps_by_runtime_flow(self):
        from robert_agent.readmodels import status

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
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
            run = status.build_run(conn, "run-1")

        step_keys = [step["step_key"] for step in run["steps"]]

        self.assertLess(step_keys.index("discover_notifications"), step_keys.index("acquire_lease"))
        self.assertLess(step_keys.index("publish_actions"), step_keys.index("reconcile"))
        self.assertLess(step_keys.index("reconcile"), step_keys.index("discover"))
        self.assertLess(step_keys.index("route"), step_keys.index("repo_pipelines"))
        self.assertLess(step_keys.index("repo_pipelines"), step_keys.index("dispatch"))

    def test_status_orders_repo_steps_by_pipeline_flow(self):
        from robert_agent.readmodels import status

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
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
            run = status.build_run(conn, "run-1")

        step_keys = [step["step_key"] for step in run["repo_steps"]]

        self.assertEqual(step_keys, ["acquire_lease", "supervise", "reconcile", "route"])

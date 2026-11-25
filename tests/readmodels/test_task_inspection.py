from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
from unittest import mock

from tests.support.task_inspection import TaskInspectionFixture


NOW = datetime(2026, 11, 24, 13, 0, tzinfo=timezone.utc)


class TaskInspectionTests(TaskInspectionFixture):
    def test_list_filters_before_limit_and_latest_before_lifecycle(self):
        from robert_agent.readmodels import tasks

        page = tasks.list_tasks(self.db, states=("completed",), query="073", limit=1, now=NOW)
        self.assertEqual(page["items"][0]["task_id"], "task-1")
        self.assertEqual(page["items"][0]["current_attempt"]["elapsed_seconds"], 120)
        self.assertEqual(tasks.list_tasks(self.db, states=("completed",), latest=True, now=NOW)["items"], [])
        self.assertEqual(tasks.list_tasks(self.db, repo="missing/repo", now=NOW)["items"], [])
        self.assertEqual(
            tasks.list_tasks(self.db, query="wi-073", latest=True, now=NOW)["items"][0]["task_id"], "task-2"
        )
        self.assertEqual(tasks.list_tasks(self.db, query="%", now=NOW)["items"], [])
        page = tasks.list_tasks(self.db, limit=1, now=NOW)
        self.assertTrue(page["has_more"])
        self.assertEqual(tasks.list_tasks(self.db, limit=1, offset=1, now=NOW)["items"][0]["task_id"], "task-1")
        page = tasks.list_tasks(self.db, since="2026-11-24T20:00:00+08:00", now=NOW)
        self.assertEqual([row["task_id"] for row in page["items"]], ["task-2"])

    def test_current_elapsed_and_active_stage_use_only_current_attempt(self):
        from robert_agent.readmodels import tasks

        row = tasks.inspect_task(self.db, "task-2", now=NOW)
        self.assertEqual(row["current_attempt"]["elapsed_seconds"], 900)
        self.assertEqual(row["current_attempt"]["heartbeat_age_seconds"], 60)
        self.assertEqual(row["phase"]["reported_age_seconds"], 600)
        self.assertEqual([run["run_id"] for run in row["active_stages"]], ["new-run"])
        self.assertEqual(row["active_stages"][0]["elapsed_seconds"], 300)
        self.assertEqual(row["active_stages"][0]["process"]["status"], "unknown")
        self.assertNotIn("old-run", json.dumps(row))

    def test_history_keeps_old_owner_and_never_uses_updated_at_as_model_completion(self):
        from robert_agent.readmodels import tasks

        current = tasks.task_stages(self.db, "task-2", now=NOW)
        self.assertEqual([run["run_id"] for run in current["items"]], ["new-run"])
        history = tasks.task_stages(self.db, "task-2", history=True, now=NOW)
        old = next(run for run in history["items"] if run["run_id"] == "old-run")
        self.assertFalse(old["current_attempt"])
        self.assertIsNone(old["elapsed_seconds"])
        self.assertIsNone(old["model_activity"]["activity_seconds"])
        self.assertNotIn("leaked-secret-value", json.dumps(history))

    def test_recorded_model_completion_separates_registration_delay(self):
        from robert_agent.readmodels import tasks

        payload = {
            "segments": {
                "segment": {
                    "launched": True,
                    "stage": "verify_final",
                    "events": {
                        "started": {"at": "2026-11-24T12:55:00Z"},
                        "model_completed": {"at": "2026-11-24T12:56:00Z"},
                        "registered": {"at": "2026-11-24T12:59:00Z"},
                    },
                }
            }
        }
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO model_usage VALUES ('stage','new-run','task-2',?,?)",
                ("2026-11-24T12:59:00Z", json.dumps(payload)),
            )
        stage = tasks.task_stages(self.db, "task-2", now=NOW)["items"][0]
        self.assertEqual(stage["model_activity"]["activity_seconds"], 60)
        self.assertEqual(stage["model_activity"]["post_model_seconds"], 180)

    def test_missing_terminal_time_and_future_heartbeat_remain_unknown(self):
        from robert_agent.readmodels import tasks

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='failed',finished_at=NULL,heartbeat_at='2026-11-24T14:00:00Z' "
                "WHERE attempt_id='attempt-2'"
            )
        row = tasks.inspect_task(self.db, "task-2", now=NOW)
        self.assertIsNone(row["current_attempt"]["elapsed_seconds"])
        self.assertIsNone(row["current_attempt"]["heartbeat_age_seconds"])
        self.assertEqual(row["active_stages"], [])

    def test_stage_log_metadata_is_bounded_and_refuses_internal_symlinks(self):
        from robert_agent.readmodels import tasks

        directory = self.root / "stage"
        directory.mkdir()
        log = directory / "stdout.log"
        log.write_bytes(b"private reasoning: never returned")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE pre_pr_review_runs SET output_dir=? WHERE run_id='new-run'", (str(directory),))
        stage = tasks.task_stages(self.db, "task-2", now=NOW)["items"][0]
        self.assertEqual(stage["logs"]["stdout.log"]["bytes"], 33)
        self.assertNotIn("private reasoning", json.dumps(stage))
        log.unlink()
        target = self.root / "other.log"
        target.write_text("still within data root")
        log.symlink_to(target)
        stage = tasks.task_stages(self.db, "task-2", now=NOW)["items"][0]
        self.assertTrue(stage["logs"]["stdout.log"]["missing"])

    def test_recorded_identity_observation_does_not_return_command_or_environment(self):
        from robert_agent.readmodels import tasks

        with closing(sqlite3.connect(self.db)) as conn, conn:
            dispatch = {"pid": 789, "command": ["dangerous-secret"], "environment": {"TOKEN": "secret"}}
            conn.execute(
                "UPDATE pre_pr_review_runs SET dispatch_json=? WHERE run_id='new-run'", (json.dumps(dispatch),)
            )
        with mock.patch.object(tasks.identity, "inspect_recorded_identity", return_value="unknown"):
            stage = tasks.task_stages(self.db, "task-2", now=NOW)["items"][0]
        self.assertEqual(stage["process"], {"pid": 789, "status": "unknown"})
        self.assertNotIn("dangerous-secret", json.dumps(stage))
        self.assertNotIn("environment", json.dumps(stage))

    def test_logs_read_registered_stderr_only_with_bounds_and_redaction(self):
        from robert_agent.readmodels import tasks

        log = self.root / "stderr.log"
        log.write_text("first\npassword=leaked-secret-value\nlast\n")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,bytes,created_at) "
                "VALUES ('stderr-2','task-2','attempt-2','worker_stderr',?,42,'2026-11-24T12:59:00Z')",
                (str(log),),
            )
        result = tasks.task_logs(self.db, "task-2", max_bytes=1024, lines=2)
        self.assertIn("Sensitive output hidden", result["items"][0]["content"])
        self.assertNotIn("leaked-secret-value", json.dumps(result))
        result = tasks.task_logs(self.db, "task-2", max_bytes=4, lines=2)
        self.assertTrue(result["items"][0]["truncated"])
        self.assertEqual(result["items"][0]["content"], "ast\n")
        log.unlink()
        log.symlink_to("/etc/passwd")
        result = tasks.task_logs(self.db, "task-2")
        self.assertTrue(result["items"][0]["missing"])

    def test_snapshots_show_recorded_history_and_current_identity(self):
        from robert_agent.readmodels import tasks

        with closing(sqlite3.connect(self.db)) as conn, conn:
            for name in ["old", "new"]:
                conn.execute(
                    "INSERT INTO pre_pr_review_snapshots VALUES (?,'round-2',?,?,?,?)",
                    (
                        name,
                        name + "head",
                        "base",
                        json.dumps(
                            {
                                "snapshot_id": name,
                                "head_sha": name + "head",
                                "base_sha": "base",
                                "diff_sha256": name + "diff",
                                "secret": "never expose arbitrary manifests",
                            }
                        ),
                        "2026-11-24T12:00:00Z",
                    ),
                )
            conn.execute("UPDATE pre_pr_review_sessions SET snapshot_id='new'")
        result = tasks.task_snapshots(self.db, "task-2")
        self.assertEqual({row["snapshot_id"] for row in result["items"]}, {"old", "new"})
        self.assertEqual(next(row for row in result["items"] if row["current"])["snapshot_id"], "new")
        self.assertNotIn("never expose", json.dumps(result))

    def test_bounds_missing_ids_and_read_only_connection(self):
        from robert_agent.readmodels import tasks

        for kwargs in [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"states": ("bogus",)}, {"since": "today"}]:
            with self.assertRaises(ValueError):
                tasks.list_tasks(self.db, **kwargs)
        self.assertIsNone(tasks.inspect_task(self.db, "missing"))
        with mock.patch.object(tasks.db_database, "connect", wraps=tasks.db_database.connect) as connect:
            tasks.list_tasks(self.db)
        self.assertIn("mode=ro", connect.call_args.args[0])
        self.assertTrue(connect.call_args.kwargs["uri"])

    def test_waiting_unstarted_stage_has_queue_age_and_no_execution_duration(self):
        from robert_agent.readmodels import tasks

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE pre_pr_review_runs SET started_at=NULL,status='waiting' WHERE run_id='new-run'")
        stage = tasks.task_stages(self.db, "task-2", now=NOW)["items"][0]
        self.assertIsNone(stage["elapsed_seconds"])
        self.assertEqual(stage["queue_elapsed_seconds"], 360)
        self.assertEqual(tasks.inspect_task(self.db, "task-2", now=NOW)["queue_seconds"], 300)

    def test_rebound_history_is_visible_from_original_task_and_run_pagination(self):
        from robert_agent.readmodels import tasks

        result = tasks.task_stages(self.db, "task-1", history=True, limit=1, now=NOW)
        self.assertEqual(result["items"][0]["owner_task_id"], "task-1")
        self.assertEqual(result["items"][0]["round_task_id"], "task-2")
        result = tasks.task_stages(self.db, "task-2", history=True, limit=1, now=NOW)
        self.assertTrue(result["has_more"])
        self.assertEqual(
            tasks.task_stages(self.db, "task-2", history=True, limit=1, offset=1, now=NOW)["items"][0]["run_id"],
            "old-run",
        )

    def test_minimal_database_unknown_metadata_and_no_attempt(self):
        from robert_agent.persistence.schema import database
        from robert_agent.readmodels import tasks

        empty = self.root / "empty.sqlite3"
        database.init_database(empty)
        self.assertEqual(tasks.list_tasks(empty)["items"], [])
        self.assertEqual(tasks.control_summary(empty)["lifecycle_counts"], {})
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET metadata_json='invalid',lifecycle='queued' WHERE task_id='task-2'")
            conn.execute("UPDATE pre_pr_review_runs SET dispatch_json='[]' WHERE run_id='new-run'")
            conn.execute("DELETE FROM worker_phases WHERE attempt_id='attempt-2'")
            conn.execute("DELETE FROM pre_pr_review_runs WHERE owner_attempt_id='attempt-2'")
            conn.execute("DELETE FROM attempts WHERE attempt_id='attempt-2'")
        row = tasks.inspect_task(self.db, "task-2", now=NOW)
        self.assertIsNone(row["current_attempt"])
        self.assertEqual(row["queue_elapsed_seconds"], 1200)
        self.assertEqual(row["waiting_reason"], "scheduler_pending")

    def test_snapshot_hashes_survive_redacted_path_collisions(self):
        from robert_agent.readmodels import tasks

        manifest = {"files": {"/data/private/diff.patch": "diff-hash", "/data/private/stat.txt": "stat-hash"}}
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO pre_pr_review_snapshots VALUES ('snap','round-2','head','base',?,'2026-11-24T12:00:00Z')",
                (json.dumps(manifest),),
            )
        files = tasks.task_snapshots(self.db, "task-2")["items"][0]["files"]
        self.assertEqual(len(files), 2)
        self.assertEqual({item["sha256"] for item in files}, {"diff-hash", "stat-hash"})
        self.assertEqual({item["name"] for item in files}, {"diff.patch", "stat.txt"})
        self.assertNotIn("/data/private/", json.dumps(files))

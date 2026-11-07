from contextlib import closing
import json
import sqlite3
from tests.support.summary_database import SummaryFixture


class ChatStatusTests(SummaryFixture):
    def test_chat_status_formats_overview(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "status")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("Robert status", payload["message"])
        self.assertIn("Tasks: 2", payload["message"])
        self.assertIn("Wakeups pending: 1", payload["message"])
        self.assertIn("Results accepted:", payload["message"])
        self.assertIn("publish_accepted_github_actions", payload["message"])
        self.assertIn("task-1", payload["message"])

    def test_chat_status_formats_task_detail(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "task", "task-1")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Task task-1", payload["message"])
        self.assertIn("Lifecycle: completed", payload["message"])
        self.assertIn(
            "Next action: publish_accepted_github_actions", payload["message"]
        )
        self.assertIn("worker_stderr", payload["message"])

    def test_chat_status_formats_run_steps(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status("--db", str(db_path), "run", "run-1")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Run run-1", payload["message"])
        self.assertIn("validate_config: succeeded", payload["message"])
        self.assertIn("dispatch: failed", payload["message"])

    def test_chat_status_formats_partial_run_repo_summaries(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE agent_runs
                SET summary_json = ?
                WHERE run_id = 'run-1'
                """,
                (
                    json.dumps(
                        {
                            "overall_status": "partial_failure",
                            "repo_summaries": [
                                {
                                    "repo_id": "repo-1",
                                    "full_name": "example/backend",
                                    "status": "failed",
                                }
                            ],
                        },
                        sort_keys=True,
                    ),
                ),
            )

        code, payload = self._run_chat_status("--db", str(db_path), "run", "run-1")

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Status: partial_failure", payload["message"])
        self.assertIn("Repos:", payload["message"])
        self.assertIn("- example/backend: failed", payload["message"])

    def test_chat_status_previews_registered_artifact(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        log_path = self.root / "worker.stderr.log"
        log_path.write_text("line 1\nworker failed clearly\n", encoding="utf-8")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE artifacts
                SET path = ?, bytes = ?
                WHERE task_id = 'task-1' AND artifact_type = 'worker_stderr'
                """,
                (str(log_path), log_path.stat().st_size),
            )

        code, payload = self._run_chat_status(
            "--db",
            str(db_path),
            "artifact",
            "task-1",
            "worker_stderr",
        )

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("Artifact worker_stderr for task-1", payload["message"])
        self.assertIn("worker failed clearly", payload["message"])

    def test_chat_status_reports_missing_task(self):
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        code, payload = self._run_chat_status(
            "--db", str(db_path), "task", "missing-task"
        )

        self.assertEqual(code, 3)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "not_found")
        self.assertIn("missing-task", payload["safe_error"])

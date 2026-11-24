from contextlib import redirect_stdout
import io
import json
from unittest import mock

from robert_agent.cli.main import main
from tests.support.task_inspection import TaskInspectionFixture


class TaskCommandTests(TaskInspectionFixture):
    def _cli(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([*args, "--db", str(self.db), "--output", "json"])
        return code, json.loads(output.getvalue())

    def test_task_commands_support_database_without_config(self):
        code, payload = self._cli(["task", "list", "--state", "running", "--query", "073"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["items"][0]["task_id"], "task-2")
        for command in ("stages", "snapshots", "logs"):
            code, payload = self._cli(["task", command, "task-2"])
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
        code, payload = self._cli(["task", "show", "task-2"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["inspection"]["current_attempt"]["attempt_id"], "attempt-2")

    def test_status_tasks_and_missing_task_have_correct_envelopes(self):
        code, payload = self._cli(["status", "--tasks", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["data"]["running_tasks"]["items"][0]["task_id"], "task-2")
        code, payload = self._cli(["task", "stages", "missing"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "not_found")

    def test_text_list_is_a_compact_table(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["task", "list", "--db", str(self.db), "--state", "running"])
        self.assertEqual(code, 0)
        self.assertIn("ELAPSED", output.getvalue())
        self.assertIn("wi-073", output.getvalue())
        self.assertIn("task-2", output.getvalue())
        self.assertNotIn("metadata_json", output.getvalue())

    def test_changed_help_has_discoverable_examples_and_bounds_fail_fast(self):
        for arguments in (
            ["status"],
            ["task", "list"],
            ["task", "show"],
            ["task", "stages"],
            ["task", "snapshots"],
            ["task", "logs"],
        ):
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main([*arguments, "--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("Examples:", output.getvalue())
        code, payload = self._cli(["task", "list", "--limit", "501"])
        self.assertNotEqual(code, 0)
        self.assertIn("1..500", payload["safe_error"])

    def test_status_quick_skips_historical_metrics_and_missing_db_is_not_created(self):
        with mock.patch("robert_agent.readmodels.status.execution_metrics.summarize") as summarize:
            code, payload = self._cli(["status", "--quick"])
        self.assertEqual(code, 0)
        self.assertIsNone(payload["data"]["execution_timings"])
        summarize.assert_not_called()
        missing = self.root / "missing.sqlite3"
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["status", "--db", str(missing), "--output", "json"])
        self.assertNotEqual(code, 0)
        self.assertFalse(missing.exists())

    def test_text_show_reports_failure_and_suppresses_sensitive_nested_metadata(self):
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET failure_json=? WHERE attempt_id='attempt-2'",
                (json.dumps({"reason": "registered prompt changed", "extra": "password=leaked-secret-value"}),),
            )
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["task", "show", "task-2", "--db", str(self.db)])
        self.assertEqual(code, 0)
        self.assertIn("registered prompt changed", output.getvalue())
        code, payload = self._cli(["task", "show", "task-2"])
        self.assertEqual(code, 0)
        self.assertNotIn("leaked-secret-value", json.dumps(payload))

    def test_status_truncation_points_to_task_list_pagination(self):
        from robert_agent.readmodels import tasks

        page = tasks.list_tasks(self.db, states=("running",), limit=1)
        page["has_more"] = True
        output = io.StringIO()
        with mock.patch.object(tasks, "list_tasks", return_value=page), redirect_stdout(output):
            code = main(["status", "--tasks", "--quick", "--limit", "1", "--db", str(self.db)])
        self.assertEqual(code, 0)
        self.assertIn("robert task list --state running --offset 1", output.getvalue())

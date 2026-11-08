from contextlib import closing
import json
import os
from unittest.mock import patch
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path


class ControlledEndToEndAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_repo = self.root / "source-repo"
        self.source_repo.mkdir()
        (self.source_repo / ".git").mkdir()
        self.source_data_dir = self.root / "source-data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""version: 2
data_dir: {self.source_data_dir}
database: dd.sqlite3
github:
  account: robot
default_worker: default
workers:
  default:
    adapter: cbc
    command: cbc
    model: gpt-5.4
    effort: high
scheduler:
  max_concurrency: 1
supervision:
  startup_grace_seconds: 1
repos:
- full_name: example/repo
  github_account: robot
  trusted_actors:
  - wklken
  checkout: {self.source_repo}
  worktrees: {self.source_repo / ".worktrees"}
  default_branch: main
""",
            encoding="utf-8",
        )

    def test_result_receipt_waits_for_process_exit_before_followup_cycle(self):
        from robert_agent.acceptance import controlled_e2e as acceptance
        from robert_agent.adapters.processes import attempt

        with (
            patch.object(
                acceptance.db_read_worker_results, "find_for_wait_for_result", return_value=[("result", "new_pr", "{}")]
            ) as read_result,
            patch.object(attempt, "dispatch_status", side_effect=[{"status": "alive_same"}, {"status": "dead"}]),
            patch.object(acceptance.time, "sleep") as sleep,
        ):
            result = acceptance._wait_for_result(self.root / "result.sqlite3", 5, 0.05)
        self.assertEqual(result, {"result_id": "result", "output_type": "new_pr"})
        self.assertEqual(read_result.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_controlled_worker_uses_the_running_package_interpreter(self):
        from robert_agent.acceptance import controlled_e2e as controlled_e2e_acceptance

        shadow_bin = self.root / "shadow-bin"
        shadow_bin.mkdir()
        wrong_python = shadow_bin / "python3"
        wrong_python.write_text("#!/bin/sh\nexit 86\n", encoding="utf-8")
        wrong_python.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(shadow_bin) + os.pathsep + os.environ["PATH"]}):
            result = controlled_e2e_acceptance.controlled_e2e_acceptance(
                self.config_path,
                workspace_dir=self.root / "interpreter-e2e",
                timeout_seconds=5,
                poll_interval_seconds=0.05,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["publish_result"]["published_count"], 2)

    def test_generated_worker_imports_source_without_an_installed_robert(self):
        from robert_agent.acceptance import controlled_e2e

        worker = controlled_e2e._mock_new_pr_worker(self.root)
        # -S excludes editable-install hooks. Add dependencies without processing
        # site .pth files, so the worker must supply the Robert source search root.
        dependencies = list({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
        bootstrap = (
            f"import sys,runpy; sys.path.extend({dependencies!r}); "
            "assert 'robert_agent' not in sys.modules; runpy.run_path(sys.argv[1], run_name='__main__')"
        )
        # An invalid identity exercises the full generated script without creating work.
        from robert_agent.context.prompts import render_prompt
        from robert_agent.context.worker_prompt import worker_prompt_input
        from robert_agent.workflows.routes import ROUTES

        prompt = render_prompt(
            worker_prompt_input(
                {"task_id": "", "attempt_id": "missing", "workstream_id": "github:example/repo!1"},
                ROUTES["new-pr"],
                [],
                runtime_context={"db_path": str(self.root / "unused.sqlite3")},
            )
        ).text
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", bootstrap, str(worker)],
            cwd=self.root,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1, result)
        self.assertTrue(result.stdout, result.stderr)
        self.assertEqual(json.loads(result.stdout)["reason_code"], "invalid_envelope")

    def test_controlled_e2e_acceptance_completes_new_pr_workflow(self):
        from robert_agent.acceptance import controlled_e2e as controlled_e2e_acceptance

        workspace_dir = (self.root / "controlled-e2e").resolve()

        result = controlled_e2e_acceptance.controlled_e2e_acceptance(
            self.config_path,
            workspace_dir=workspace_dir,
            timeout_seconds=5,
            poll_interval_seconds=0.05,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route_id"], "new-pr")
        self.assertEqual(result["task_lifecycle"], "completed")
        self.assertEqual(result["issue_workstream_lifecycle"], "completed")
        self.assertEqual(result["derived_pr_workstream_lifecycle"], "completed")
        self.assertEqual(result["worker_result"]["output_type"], "new_pr")
        self.assertEqual(result["publish_result"]["published_count"], 2)
        self.assertEqual(result["publish_result"]["finalized_task_count"], 1)
        self.assertEqual(
            result["github_actions"],
            [("push_existing_pr", "accepted", "published"), ("open_pr", "accepted", "published")],
        )
        self.assertEqual(result["published_pr_url"], "https://github.com/example/repo/pull/42")
        self.assertTrue(Path(result["worktree_path"]).is_dir())
        self.assertFalse((self.source_data_dir / "dd.sqlite3").exists())
        with closing(sqlite3.connect(result["db_path"])) as conn, conn:
            active_tasks = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE lifecycle IN ('queued', 'running')"
            ).fetchone()[0]
        self.assertEqual(active_tasks, 0)


if __name__ == "__main__":
    unittest.main()

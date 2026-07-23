from contextlib import closing
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class LiveWorkerAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.worktree_root.mkdir()
        self.source_data_dir = self.root / "source-data"
        self.worker_command = self.root / "mock-worker.py"
        self.worker_command.write_text(
            f"""#!/usr/bin/env python3
import json
import sys

sys.path.insert(0, {str(PACKAGE_ROOT.parent)!r})
from robert_agent.worker import result

prompt = sys.stdin.read()
fields = {{}}
for line in prompt.splitlines():
    if ": " in line:
        key, value = line.split(": ", 1)
        fields[key] = value
fingerprints = json.loads(fields["event_fingerprints"])
payload = {{
    "task_id": fields["task_id"],
    "attempt_id": fields["attempt_id"],
    "output_type": "comment_analysis",
    "planned_github_actions": [
        {{
            "type": "comment",
            "target_url": "https://github.com/example/repo/issues/77",
            "body": "<!-- robert-comment task_id={{}} attempt_id={{}} event_fingerprints={{}} -->\\nAcceptance analysis is ready".format(
                fields["task_id"],
                fields["attempt_id"],
                ",".join(fingerprints),
            ),
        }}
    ],
    "consumed_event_fingerprints": fingerprints,
    "verification": [
        {{
            "command": "mock-worker",
            "status": "passed",
            "purpose": "record isolated live worker acceptance result",
            "required": True,
            "exit_code": 0,
        }}
    ],
    "handoff": "acceptance ready",
    "used_skills": [],
}}
record = result.record_result(fields["db_path"], payload)
print(json.dumps(record, sort_keys=True))
raise SystemExit(0 if record["ok"] else 1)
""",
            encoding="utf-8",
        )
        os.chmod(self.worker_command, 0o755)
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""data_dir: {self.source_data_dir}
database: dd.sqlite3
worker_command: {self.worker_command}
max_concurrency: 1
worker_startup_grace_seconds: 1
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: main
    repo_root: {self.repo_root}
    worktree_root: {self.worktree_root}
""",
            encoding="utf-8",
        )

    def test_live_worker_acceptance_uses_isolated_state_and_dry_run_publication(self):
        from robert_agent import live_worker_acceptance
        workspace_dir = (self.root / "acceptance-workspace").resolve()

        result = live_worker_acceptance.live_worker_acceptance(
            self.config_path,
            workspace_dir=workspace_dir,
            timeout_seconds=5,
            poll_interval_seconds=0.05,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route_id"], "comment-analysis")
        self.assertEqual(result["worker_result"]["output_type"], "comment_analysis")
        self.assertEqual(result["action_counts"], {"accepted": 1, "not_published": 1})
        self.assertEqual(result["dry_run_publication"]["status"], "dry_run")
        self.assertEqual(result["dry_run_publication"]["pending_count"], 1)
        db_path = Path(result["db_path"])
        self.assertTrue(str(db_path).startswith(str(workspace_dir)))
        self.assertTrue(db_path.exists())
        self.assertFalse((self.source_data_dir / "dd.sqlite3").exists())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(attempt_status, "completed")
        self.assertEqual(action_status, ("accepted", "not_published"))

    def test_isolated_config_preserves_named_workers_and_route_selection(self):
        from robert_agent import live_worker_acceptance
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                f"worker_command: {self.worker_command}",
                "workers:\n"
                "  - name: default\n"
                "    agent: cbc\n"
                f"    command: {self.worker_command}\n"
                "    default_model: gpt-5.4\n"
                "    default_effort: high\n"
                "  - name: reviewer\n"
                "    agent: codex\n"
                "    command: /opt/bin/review-codex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: xhigh\n"
                "route_worker_models:\n"
                "  review-pr:\n"
                "    worker: reviewer\n"
                "    effort: high",
            ),
            encoding="utf-8",
        )
        workspace_dir = (self.root / "isolated-config").resolve()
        workspace_dir.mkdir()

        result = live_worker_acceptance._isolated_config(
            self.config_path,
            workspace_dir,
        )

        self.assertTrue(result["ok"], result)
        isolated = json.loads(Path(result["config_path"]).read_text(encoding="utf-8"))
        self.assertEqual([worker["name"] for worker in isolated["workers"]], ["default", "reviewer"])
        self.assertEqual(isolated["workers"][1]["agent"], "codex")
        self.assertEqual(
            isolated["route_worker_models"]["review-pr"],
            {
                "worker": "reviewer",
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        )


if __name__ == "__main__":
    unittest.main()

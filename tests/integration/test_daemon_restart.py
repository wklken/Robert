"""Acceptance gaps left by the first reliability delivery."""

from tests.support import TEST_ROOT


from contextlib import closing

import sqlite3

import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


class IsolatedDaemonTests(unittest.TestCase):
    def test_daemon_restart_reconciles_real_git_write_and_delivers_notification(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time
        from tests.support.publication_budget import PublicationBudgetFixture
        from robert_agent.persistence import execution_control as execution_store
        from robert_agent.work.application import source_events

        fixture = PublicationBudgetFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = Path(fixture.tmp.name)

        def git(*args):
            return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-b", "main")
        (root / "example.txt").write_text("isolated acceptance\n")
        git("add", "example.txt")
        git(
            "-c",
            "user.name=Acceptance",
            "-c",
            "user.email=acceptance@example.invalid",
            "commit",
            "-m",
            "Isolated fixture",
        )
        sha = git("rev-parse", "HEAD")
        git("init", "--bare", str(root / "remote.git"))
        git("remote", "add", "origin", str(root / "remote.git"))
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])
            metadata["audit"]["action_scope"]["result_head_sha"] = sha
            conn.execute("UPDATE worker_results SET metadata_json=?", (json.dumps(metadata),))
            conn.execute("UPDATE repos SET repo_id='repo:x/y' WHERE repo_id='repo'")
            conn.execute("UPDATE workstreams SET repo_id='repo:x/y',active_task_id='t'")
            execution_store.ensure_subject(conn, "task", "t", "human_request", fixture.now.isoformat())
            # This publication has a persisted, independently trusted request.
            # The daemon's configured publisher now reauthorizes that trigger.
            event = {
                "event_type": "comment",
                "event_fingerprint": "comment:restart-request",
                "source_key": "github:x/y#1",
                "source_type": "issue",
                "number": 1,
                "workstream_id": "ws",
                "actor_login": "maintainer",
                "body": "@isolated implement this",
                "body_authorization_complete": True,
            }
            _, event_id = source_events.insert_source_and_event(conn, "repo:x/y", event, fixture.now.isoformat())
            conn.execute(
                "INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('t',?,'trigger',?)",
                (event_id, fixture.now.isoformat()),
            )
            conn.execute(
                "INSERT INTO notifications(notification_id,task_id,notification_type,channel,status,created_at) VALUES ('notice','t','isolated','local','recorded',?)",
                (fixture.now.isoformat(),),
            )
        (root / "daemon.yml").write_text(f"""version: 2
data_dir: {root}
database: db
github:
  account: isolated
default_worker: default
workers:
  default:
    adapter: cbc
    command: cbc
    model: gpt-5.4
    effort: high
scheduler:
  max_concurrency: 1
  local_poll_seconds: 1
daemon:
  enabled: true
repos:
- full_name: x/y
  github_account: isolated
  trusted_actors:
  - maintainer
  checkout: {root}
  worktrees: {root}/worktrees
  default_branch: main
""")
        (root / "receiver.py").write_text(
            'import sys,json\nfrom pathlib import Path\np=Path(__file__).with_name("deliveries.jsonl")\nv=json.load(sys.stdin)\nwith p.open("a") as f:f.write(json.dumps(v)+"\\n")\n'
        )
        import shlex

        env = {
            **os.environ,
            "GH_CONFIG_DIR": str(root / "empty-gh-config"),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "ROBERT_NOTIFICATION_COMMAND": shlex.join([sys.executable, str(root / "receiver.py")]),
        }
        child = str(TEST_ROOT / "support" / "reliability_daemon_child.py")
        with (root / "daemon.log").open("w") as log:
            first = subprocess.Popen([sys.executable, child, str(root), "crash"], stdout=log, stderr=log, env=env)

            def stop(process):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)

            self.addCleanup(stop, first)
            self.assertEqual(first.wait(timeout=30), 73, (root / "daemon.log").read_text())
            with closing(sqlite3.connect(fixture.db)) as conn, conn:
                self.assertEqual(conn.execute("SELECT status FROM publication_intents").fetchone()[0], "executing")
                # Simulate the lease grace period after confirmed process death.
                conn.execute(
                    "UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE resource_type IN ('daemon','agent_run')"
                )
            second = subprocess.Popen([sys.executable, child, str(root), "recover"], stdout=log, stderr=log, env=env)
            self.addCleanup(stop, second)
            deadline = time.monotonic() + 30
            complete = False
            while time.monotonic() < deadline and second.poll() is None:
                with closing(sqlite3.connect(fixture.db)) as conn:
                    complete = conn.execute("SELECT status FROM publication_intents").fetchone()[0] == "published"
                    binding = conn.execute("SELECT binding_status FROM execution_subjects").fetchone()[0]
                if complete and binding == "terminal":
                    break
                time.sleep(0.1)
            self.assertTrue(complete and binding == "terminal", (root / "daemon.log").read_text())
            second.terminate()
            self.assertEqual(second.wait(timeout=15), 0, (root / "daemon.log").read_text())
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum(command[:2] == ["git", "push"] for command in calls), 1)
        self.assertIn(sha, git("ls-remote", "origin", "refs/heads/codex/fix"))
        deliveries = [json.loads(line) for line in (root / "deliveries.jsonl").read_text().splitlines()]
        self.assertEqual(len([v for v in deliveries if v["type"] == "isolated"]), 1)
        with closing(sqlite3.connect(fixture.db)) as conn:
            delivery = json.loads(
                conn.execute("SELECT metadata_json FROM notifications WHERE notification_id='notice'").fetchone()[0]
            )["delivery"]
            self.assertEqual(delivery["status"], "sent")

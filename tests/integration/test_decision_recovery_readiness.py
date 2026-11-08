"""A durable model grant makes its local recovery consumer ready."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
import time
from textwrap import dedent
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import yaml
from robert_agent.adapters.processes import identity
from robert_agent.configuration.command import validate_config
from robert_agent.execution import runtime_state
from robert_agent.runtime import cycle, daemon
from robert_agent.supervision import decisions
from robert_agent.supervision.application.decision_loop import DecisionLoop
from tests.support.repository_cycle import RepositoryCycleFixture

REPO = "repo:example/backend"


class DecisionRecoveryReadinessTests(unittest.TestCase):
    def test_approved_retry_wakes_local_drain_without_a_scan(self):
        f = RepositoryCycleFixture()
        f.setUp()
        loop = DecisionLoop()
        config = None
        try:
            db, task, _ = f._interrupted_attempt(phase=False, transport=True)
            ready = f.root / "ready"
            dead = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(20)",
                ],
                start_new_session=True,
            )
            try:
                timeout = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < timeout:
                    time.sleep(0.01)
                observed = identity.capture_process_identity(dead.pid)
            finally:
                identity.cleanup_new_process(dead)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute(
                    "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.dispatch',json(?))",
                    (json.dumps(observed),),
                )
            model_script = f.root / "decision-model.py"
            model_script.write_text(
                dedent("""
    import json, sys
    p = json.load(sys.stdin)
    candidate = next(c for c in p['candidates'] if c['action'] == 'retry_worker')
    json.dump(dict(bundle_id=p['bundle_id'],bundle_sha256=p['bundle_sha256'],action='retry_worker',
    candidate_id=candidate['id'],reason='Controlled dead Worker', evidence_ids=['tasks'],
    expected_progress='successor attempt', recheck_seconds=300, message=''),sys.stdout)
    """)
            )
            raw = yaml.safe_load(f.config_path.read_text())
            raw["supervision"].update(mode="decide", decision={"command": [sys.executable, str(model_script)]})
            raw["maintenance"] = {"enabled": False}
            f.config_path.write_text(yaml.safe_dump(raw))
            config = validate_config(f.config_path, skip_external=True)
            assert config["ok"], config
            assert cycle.run_once(f.config_path, dry_run=True, skip_external=True, skip_publish=True)["ok"]

            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()
            with closing(sqlite3.connect(db)) as conn:
                root = conn.execute("SELECT workstream_id FROM tasks WHERE task_id=?", (task,)).fetchone()[0]
                wait = conn.execute("SELECT json_extract(failure_json,'$.status') FROM attempts").fetchone()[0]
                assert wait == "waiting_for_model_decision", wait
            with closing(runtime_state.connect(db)) as conn, conn:
                run_id = runtime_state.start_daemon_run(
                    conn, config_path=str(f.config_path), owner_id="probe", now=now
                )["daemon_run_id"]

            context = daemon.DaemonContext(
                str(f.config_path),
                None,
                db,
                REPO,
                config["max_concurrency"],
                config["daemon"],
                run_id,
                "probe-lease",
                repo_ids=[REPO],
                runtime_config=config,
                dry_run=True,
            )
            context.startup_live_poll_pending = False
            context.next_live_poll_at = (now_dt + timedelta(seconds=60)).isoformat()
            context.background_lanes = {
                name: SimpleNamespace(process=None, poll=Mock(return_value=None), start=Mock())
                for name in ("scan", "publish")
            }
            drains = []

            def safe_child(command, **kwargs):
                assert "--consume-observations" in command, command
                result = cycle.run_once(
                    f.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
                )
                drains.append(result)
                return result

            before = daemon.run_once_decision(context, now=now, child_runner=safe_child)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                model_result = loop.poll(config, now=now)
                if model_result["status"] == "decided":
                    break
                time.sleep(0.03)
            else:
                raise AssertionError(model_result)
            with closing(runtime_state.connect(db)) as conn:
                approval = decisions.read(conn, root)["status"]
                with closing(sqlite3.connect(db)) as raw:
                    wakes = raw.execute("SELECT count(*) FROM wakeups WHERE status='pending'").fetchone()[0]
                check_now = (now_dt + timedelta(seconds=1)).isoformat()
                self.assertTrue(decisions.has_ready_worker_retry(conn, config, [REPO], check_now))
                expired = (now_dt + timedelta(days=2)).isoformat()
                self.assertFalse(decisions.has_ready_worker_retry(conn, config, [REPO], expired))
                self.assertFalse(
                    decisions.has_ready_worker_retry(conn, {**config, "supervision": {"mode": "observe"}}, [REPO], now)
                )
                with closing(sqlite3.connect(db)) as raw, raw:
                    raw.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (task,))
                self.assertFalse(decisions.has_ready_worker_retry(conn, config, [REPO], check_now))
                with closing(sqlite3.connect(db)) as raw, raw:
                    raw.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task,))
            after = daemon.run_once_decision(
                context, now=(now_dt + timedelta(seconds=1)).isoformat(), child_runner=safe_child
            )
            assert approval == "approved" and wakes == 0
            assert before["decision"] == "idle"
            assert context.background_lanes["scan"].start.call_count == 0
            assert after["decision"] == "local_drain", after
            assert len(drains) == 1
            with closing(sqlite3.connect(db)) as conn:
                statuses = conn.execute("SELECT status FROM attempts ORDER BY attempt_no").fetchall()
            assert statuses == [("failed",), ("prepared",)], statuses
        finally:
            if config and config.get("ok"):
                loop.shutdown(config)
            f.doCleanups()

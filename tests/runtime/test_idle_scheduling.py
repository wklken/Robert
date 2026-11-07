from robert_agent.adapters.processes import attempt as attempt_process
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tests import support  # noqa: F401
from robert_agent.runtime import daemon
from robert_agent.runtime import loop as loop_engine
from robert_agent.configuration import command as validate_config


class IdleSchedulingTests(unittest.TestCase):
    def test_fractional_deadline_waits_instead_of_spinning(self):
        now = datetime.now(timezone.utc)
        context = SimpleNamespace(
            daemon_config={"local_poll_seconds": 5},
            background_lanes=None,
            control_attention={},
            control_retry_at={},
            next_live_poll_at=(now + timedelta(seconds=0.4)).isoformat(),
        )
        waits = []
        daemon._wait_for_next_decision(
            context, {"decision": "idle"}, now=now, sleeper=waits.append
        )
        self.assertEqual(waits, [0.4])

    def fixture(self):
        from tests.support.scheduler_remediation import SchedulerRemediationFixture

        fixture = SchedulerRemediationFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config.write_text(
            fixture.config.read_text().replace(
                "max_concurrency: 3", "max_concurrency: 1"
            )
        )
        now = fixture.now.isoformat()
        with fixture.connect() as conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('waiting','repo:example/repo','active',?,?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('waiting','waiting','queued',?,?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at) VALUES ('waiting','waiting',1,'prepared',?,?)",
                (now, now),
            )
        return fixture

    def test_full_capacity_prepared_work_does_not_create_runs(self):
        fixture = self.fixture()
        result = loop_engine.run_loop(
            fixture.config, dry_run=False, skip_external=True, skip_publish=True
        )
        self.assertEqual(result["cycles"], 0)
        with fixture.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM agent_runs").fetchone()[0], 0
            )

    def test_capacity_gate_preserves_supervision_and_manual_wakeups(self):
        fixture = self.fixture()
        config = validate_config.validate_config(fixture.config, skip_external=True)
        self.assertFalse(fixture.runnable(config_result=config))
        fixture.stop_process()
        self.assertTrue(fixture.runnable(config_result=config))
        with patch.object(
            attempt_process, "dispatch_status", return_value={"status": "alive_same"}
        ):
            with fixture.connect() as conn:
                now = fixture.now.isoformat()
                conn.execute(
                    "INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,status,not_before_at,created_at,updated_at) VALUES ('manual','repo:example/repo','manual_operator_request','manual','pending',?,?,?)",
                    (now, now, now),
                )
            self.assertTrue(fixture.runnable(config_result=config))

    def test_repo_capacity_does_not_block_another_repository(self):
        fixture = self.fixture()
        config = validate_config.validate_config(fixture.config, skip_external=True)
        config["max_concurrency"] = 2
        config["repos"][0]["max_concurrency"] = 1
        self.assertFalse(fixture.runnable(config_result=config))
        with fixture.connect() as conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('other','example/other','robot','main','/repo','/worktrees')"
            )
            conn.execute(
                "UPDATE workstreams SET repo_id='other' WHERE workstream_id='waiting'"
            )
            self.assertTrue(
                loop_engine.has_runnable_local_work(
                    conn, repo_ids=["repo:example/repo", "other"], config_result=config
                )
            )

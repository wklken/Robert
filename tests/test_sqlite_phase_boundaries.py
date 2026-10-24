from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from robert_agent import execution_store, loop_engine, route, run_once, workstream
from tests import test_pr_remediation_integration, test_run_once


class TimeoutPhaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_run_once.RunOnceDryRunTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        result = run_once.run_once(
            self.fixture.config_path,
            fixture_path=self.fixture.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        self.db = self.fixture.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(self.db)) as conn:
            self.task, self.attempt = conn.execute(
                "SELECT task_id,attempt_id FROM attempts"
            ).fetchone()

    def cycle(self, now):
        with mock.patch.object(run_once, "_now", return_value=now):
            result = run_once.run_once(
                self.fixture.config_path,
                dry_run=True,
                skip_external=True,
                skip_publish=True,
            )
        self.assertTrue(result["ok"], result)
        return result

    def fail_timeout(self, conn, now):
        conn.execute(
            "UPDATE attempts SET status='failed',finished_at=?,failure_json=?",
            (now, json.dumps({"status": "failed_timeout"})),
        )
        conn.execute("UPDATE tasks SET lifecycle='failed'")
        conn.execute("UPDATE workstreams SET lifecycle='failed',active_task_id=NULL")

    def test_fresh_timeout_survives_until_next_recovery_pass_and_keeps_quota(self):
        now = "2026-10-24T00:00:00+00:00"

        def timeout(conn, *_args, **_kwargs):
            self.fail_timeout(conn, now)
            return 1

        with mock.patch.object(
            run_once, "_supervise_running_attempts", side_effect=timeout
        ):
            self.cycle(now)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            control = execution_store.get_binding(conn, "task", self.task)
            self.assertEqual(control["binding_status"], "active")
            self.assertEqual(control["recovery_attempts"], 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1
            )
            next_pass_runnable = loop_engine.has_runnable_local_work(conn)

        evidence = {
            "status": "recoverable_progress",
            "task_id": self.task,
            "previous_attempt_id": self.attempt,
            "progress_signals": ["dirty_worktree"],
        }
        with (
            mock.patch.object(
                run_once, "_dispatch_process_status", return_value={"status": "dead"}
            ),
            mock.patch.object(
                run_once,
                "_inspect_attempt_recovery_with_metadata",
                return_value=evidence,
            ),
        ):
            self.cycle("2026-10-24T00:00:01+00:00")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            control = execution_store.get_binding(conn, "task", self.task)
            self.assertEqual(control["binding_status"], "active")
            self.assertEqual(control["recovery_attempts"], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2
            )
            latest = conn.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_no DESC LIMIT 1"
            ).fetchone()[0]
            self.assertNotEqual(latest, self.attempt)
            self.assertIn(
                conn.execute("SELECT lifecycle FROM tasks").fetchone()[0],
                {"queued", "running"},
            )
            self.fail_timeout(conn, "2026-10-24T00:00:02+00:00")

        evidence["previous_attempt_id"] = latest
        with (
            mock.patch.object(
                run_once, "_dispatch_process_status", return_value={"status": "dead"}
            ),
            mock.patch.object(
                run_once,
                "_inspect_attempt_recovery_with_metadata",
                return_value=evidence,
            ),
        ):
            self.cycle("2026-10-24T00:00:03+00:00")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            control = execution_store.get_binding(conn, "task", self.task)
            self.assertEqual(control["binding_status"], "attention")
            self.assertEqual(control["recovery_attempts"], 1)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2
            )
            self.assertFalse(loop_engine.has_runnable_local_work(conn))
        self.assertTrue(
            next_pass_runnable, "A fresh timeout must schedule its recovery pass"
        )

    def test_unavailable_timeout_recovery_stops_with_attention(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.fail_timeout(conn, "2026-10-24T00:00:00+00:00")
        with (
            mock.patch.object(
                run_once, "_dispatch_process_status", return_value={"status": "dead"}
            ),
            mock.patch.object(
                run_once,
                "_inspect_attempt_recovery_with_metadata",
                return_value={"status": "no_progress"},
            ),
        ):
            self.cycle("2026-10-24T00:00:01+00:00")
            self.cycle("2026-10-24T00:00:02+00:00")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            control = execution_store.get_binding(conn, "task", self.task)
            self.assertEqual(control["binding_status"], "attention")
            self.assertEqual(control["recovery_attempts"], 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed"
            )
            self.assertFalse(loop_engine.has_runnable_local_work(conn))


class RemediationPhaseBoundaryTests(unittest.TestCase):
    def test_fresh_health_retries_failed_episode_once_without_resetting_budget(self):
        fixture = test_pr_remediation_integration.PrRemediationIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        (Path(fixture.tmp.name) / ".git").mkdir()
        fixture.repo["repo_root"] = fixture.tmp.name
        fixture.repo["worktree_root"] = fixture.tmp.name
        fixture.repo["pr_automation"]["ci"]["enabled"] = False
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            event = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                fixture.repo,
                fixture.runner(),
                "2026-07-26T00:01:00+00:00",
            )[0]
            episode = event["metadata"]["remediation"]["episode_id"]
            task = run_once._create_task_attempt_and_prompt(
                conn,
                Path(fixture.tmp.name),
                fixture.db_path,
                fixture.repo,
                "repo:example/backend",
                event,
                route.route_task(event),
                workstream.plan_event(event, active_workstreams=set()),
                "2026-07-26T00:01:00+00:00",
                worktree_result=run_once._prepare_worktree(
                    fixture.repo, event, route.route_task(event), True
                ),
            )
            run_once._finalize_failed_task(
                conn,
                task["task_id"],
                task["workstream_id"],
                "2026-07-26T00:02:00+00:00",
                {"status": "worker_failed"},
            )
            conn.execute("UPDATE attempts SET status='failed'")

        skill_root = Path(fixture.tmp.name) / "skills"
        for name in ("fast-conflict-fix", "fast-preflight"):
            path = skill_root / name
            path.mkdir(parents=True)
            (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nFixture skill")
        config_path = Path(fixture.tmp.name) / "config.yml"
        config_path.write_text(
            json.dumps(
                {
                    "data_dir": fixture.tmp.name,
                    "database": fixture.db_path.name,
                    "repos": [fixture.repo],
                    "skills": {"search_paths": [str(skill_root)]},
                }
            )
        )
        config = run_once.validate_config.validate_config(
            config_path, skip_external=True
        )
        self.assertTrue(config["ok"], config)
        with (
            mock.patch.object(
                run_once.validate_config, "validate_config", return_value=config
            ),
            mock.patch.object(
                run_once.discover, "collect_account_notifications", return_value={}
            ),
            mock.patch.object(
                run_once.discover, "collect_live_events", return_value=[]
            ),
            mock.patch.object(
                run_once, "_reconcile_remote_source_states", return_value=0
            ),
        ):
            for now in ("2026-07-26T00:03:00+00:00", "2026-07-26T00:04:00+00:00"):
                with mock.patch.object(run_once, "_now", return_value=now):
                    result = run_once.run_once(
                        config_path,
                        dry_run=True,
                        skip_publish=True,
                        discovery_runner=fixture.runner(),
                    )
                self.assertTrue(result["ok"], result)
                with closing(sqlite3.connect(fixture.db_path)) as conn:
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2
                    )
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 2
                    )
                    row = conn.execute(
                        "SELECT status,attempt_count,observed_head_sha,observed_base_sha FROM pr_remediation_episodes WHERE episode_id=?",
                        (episode,),
                    ).fetchone()
                    self.assertEqual(row, ("remediating", 2, "head-1", "base-1"))

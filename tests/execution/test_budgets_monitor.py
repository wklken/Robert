"""A durable pre-PR plan bounds runtime while heartbeat supervision stays live."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.execution import ownership, rounds, stages
from robert_agent.execution.application import dispatch, monitor, readiness
from robert_agent.persistence.rounds import pre_pr_review, review
from robert_agent.persistence.schema import database
from tests.support import database as fixture_database
from tests.support.temp_workspace import TemporaryWorkspace


def review_plan():
    def profile(name, timeout):
        return {
            "name": name,
            "agent": "command",
            "command": "test-worker",
            "command_argv": ["test-worker"],
            "default_model": "test-model",
            "default_effort": "high",
            "fallback_model": None,
            "environment_allowlist": [],
            "timeout_seconds": timeout,
        }

    return {
        "timeout_seconds": 4200,
        "implementer": profile("coder", 4200),
        "workers": [profile("reviewer", 4200), profile("other-reviewer", 3600)],
    }


class PipelineBudgetTests(unittest.TestCase):
    def test_single_round_budget_covers_implementation_review_and_one_repair(self):
        from robert_agent.execution.budgets import pipeline_timeout

        plan = {**review_plan(), "review_policy": "single_review_single_repair"}
        self.assertEqual(pipeline_timeout(plan), 2 * 4200 + 4200 + 4200 + 3600 + 6 * 120)
        self.assertEqual(pipeline_timeout(plan), 21120)

    def test_single_round_does_not_reserve_verifier_or_second_repair_time(self):
        from robert_agent.execution.budgets import pipeline_timeout

        plan = {
            "review_policy": "single_review_single_repair",
            "timeout_seconds": 100,
            "implementer": {"timeout_seconds": 80},
            "workers": [{"timeout_seconds": 50}, {"timeout_seconds": 200}],
        }
        self.assertEqual(pipeline_timeout(plan), 2 * 80 + 200 + 50 + 100 + 6 * 80)

    def test_unknown_review_policy_cannot_supply_an_extended_budget(self):
        from robert_agent.execution.budgets import pipeline_timeout

        with self.assertRaisesRegex(ValueError, "unsupported review policy"):
            pipeline_timeout({**review_plan(), "review_policy": "unlimited"})

    def test_envelope_covers_one_repair_and_parallel_reviews(self):
        from robert_agent.execution.budgets import pipeline_timeout

        plan = review_plan()
        self.assertEqual(pipeline_timeout(plan), 3 * 4200 + 4200 + 3 * 4200 + 3600 + 9 * 120)
        plan["workers"].append({"timeout_seconds": 4200})
        self.assertEqual(pipeline_timeout(plan), 34080)

    def test_envelope_covers_actual_worker_limits_above_plan_timeout(self):
        from robert_agent.execution.budgets import pipeline_timeout

        plan = {
            "timeout_seconds": 100,
            "implementer": {"timeout_seconds": 80},
            "workers": [{"timeout_seconds": 50}, {"timeout_seconds": 200}],
        }
        self.assertEqual(pipeline_timeout(plan), 3 * 80 + 200 + 3 * 50 + 100 + 9 * 80)
        self.assertEqual(plan["workers"][1]["timeout_seconds"], 200)

    def test_invalid_plan_cannot_supply_a_runtime_budget(self):
        from robert_agent.execution.budgets import pipeline_timeout

        for plan in (
            {},
            {**review_plan(), "workers": []},
            {**review_plan(), "timeout_seconds": 0},
            {**review_plan(), "implementer": {"timeout_seconds": True}},
            {**review_plan(), "workers": [{"timeout_seconds": float("inf")}]},
        ):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                pipeline_timeout(plan)

    def test_handoff_repairs_add_only_a_fixed_capped_allowance(self):
        from robert_agent.execution.budgets import pipeline_timeout

        plan = review_plan()
        plan["handoff_repair_attempts"] = 1000000
        plan["pipeline_timeout_seconds"] = 1000000
        self.assertEqual(pipeline_timeout(plan), 34080)


class PipelineMonitorTests(TemporaryWorkspace):
    def setUp(self):
        super().setUp()
        self.db = self.root / "test.sqlite3"
        database.init_database(self.db)
        self.now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        self.config = {
            "hard_timeout_seconds": 4200,
            "heartbeat_stale_seconds": 1200,
            "max_concurrency": 3,
            "repos": [{"full_name": "x/y"}],
        }
        timestamp = self.now.isoformat()
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES('repo','x/y','bot','main','/repo','/trees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) "
                "VALUES('stream','repo','active','task',?,?)",
                (timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                "VALUES('task','stream','running',?,?)",
                (timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) "
                "VALUES('attempt','task',1,'running',?,?,?)",
                (timestamp, timestamp, json.dumps({"result_contract": "compact-v1"})),
            )
            conn.execute(
                "INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,next_step,created_at) "
                "VALUES('phase','attempt','verify','running','review in progress','',?)",
                (timestamp,),
            )
            self.assertTrue(ownership.claim_task(conn, "task", timestamp))

    def observe(self, *, runtime, heartbeat_age=0, plan=None, coordinator=True, launch_timeout=1000000, payload=None):
        metadata = {
            "result_contract": "compact-v1",
            "dispatch": {
                "pid": 123456789,
                "model_coordinator": coordinator,
                "worker_launch": {"timeout_seconds": launch_timeout},
            },
            **(payload or {}),
        }
        with fixture_database.connect(self.db) as conn:
            if plan is not None:
                pre_pr_review.create(conn, "task", {"plan": plan})
            conn.execute(
                "UPDATE attempts SET started_at=?,heartbeat_at=?,metadata_json=? WHERE attempt_id='attempt'",
                (
                    (self.now - timedelta(seconds=runtime)).isoformat(),
                    (self.now - timedelta(seconds=heartbeat_age)).isoformat(),
                    json.dumps(metadata),
                ),
            )
        with (
            patch.object(attempt_process, "dispatch_status", return_value={"status": "alive_same"}),
            patch.object(attempt_process, "terminate_attempt", return_value={"status": "terminated"}) as terminate,
            patch.object(rounds, "terminate_children", return_value="terminated"),
            fixture_database.connect(self.db) as conn,
        ):
            due = readiness.has_local_work(
                conn, ["repo"], config_result=self.config, now=self.now, include_publication=False
            )
            count = monitor.supervise_running_attempts(
                conn,
                "repo",
                self.config,
                self.now.isoformat(),
                resume_attempt=None,
                allow_recovery=False,
            )
            row = conn.execute("SELECT status,failure_json FROM attempts WHERE attempt_id='attempt'").fetchone()
            return due, count, row["status"], json.loads(row["failure_json"] or "{}"), terminate.call_count

    def test_verified_pipeline_with_fresh_heartbeat_survives_seventy_minutes(self):
        self.assertEqual(self.observe(runtime=4201, plan=review_plan()), (False, 0, "running", {}, 0))

    def test_single_round_durable_plan_uses_its_shorter_deadline(self):
        plan = {**review_plan(), "review_policy": "single_review_single_repair"}
        due, count, status, failure, stopped = self.observe(runtime=21120, plan=plan)
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], 21120)

    def test_single_round_fresh_heartbeat_before_deadline_still_runs(self):
        plan = {**review_plan(), "review_policy": "single_review_single_repair"}
        self.assertEqual(self.observe(runtime=21119, plan=plan), (False, 0, "running", {}, 0))

    def test_pipeline_does_not_extend_heartbeat_failure_timeout(self):
        due, count, status, failure, stopped = self.observe(runtime=5000, heartbeat_age=4200, plan=review_plan())
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["status"], "failed_timeout")

    def test_worker_payload_cannot_extend_an_ordinary_task(self):
        due, count, status, failure, stopped = self.observe(
            runtime=4201,
            payload={"pre_pr_review": {"plan": review_plan()}, "pipeline_timeout_seconds": 1000000},
        )
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], 4200)

    def test_durable_plan_without_coordinator_marker_keeps_ordinary_timeout(self):
        due, count, status, failure, stopped = self.observe(runtime=4201, plan=review_plan(), coordinator=False)
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], 4200)

    def test_ordinary_worker_timeout_still_caps_runtime(self):
        due, count, status, failure, stopped = self.observe(runtime=61, coordinator=False, launch_timeout=60)
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], 60)

    def test_pipeline_stops_at_recomputed_envelope_with_fresh_heartbeat(self):
        from robert_agent.execution.budgets import pipeline_timeout

        due, count, status, failure, stopped = self.observe(runtime=pipeline_timeout(review_plan()), plan=review_plan())
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], pipeline_timeout(review_plan()))

    def test_fresh_heartbeat_remains_running_just_before_envelope(self):
        from robert_agent.execution.budgets import pipeline_timeout

        self.assertEqual(
            self.observe(runtime=pipeline_timeout(review_plan()) - 1, plan=review_plan()),
            (False, 0, "running", {}, 0),
        )

    def test_malformed_durable_plan_keeps_global_timeout(self):
        due, count, status, failure, stopped = self.observe(runtime=4201, plan={"timeout_seconds": 1000000})
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], 4200)

    def test_native_existing_pr_review_keeps_original_global_timeout(self):
        with fixture_database.connect(self.db) as conn:
            entry = review.enqueue(
                conn,
                {"full_name": "x/y"},
                {"number": 42, "workstream_id": "stream", "event_fingerprint": "native-request"},
                {},
                review_plan()["workers"],
            )
            conn.execute("UPDATE review_sessions SET task_id='task' WHERE round_id=?", (entry["round_id"],))
        due, count, status, failure, stopped = self.observe(runtime=4201)
        self.assertTrue(due)
        self.assertEqual((count, status, stopped), (1, "failed", 1))
        self.assertEqual(failure["timeout_seconds"], 4200)

    def test_pipeline_stale_heartbeat_stays_nonterminal_before_global_timeout(self):
        due, count, status, failure, stopped = self.observe(runtime=5000, heartbeat_age=1200, plan=review_plan())
        self.assertTrue(due)
        self.assertEqual((count, status, failure, stopped), (1, "stale", {}, 0))

    def test_model_stage_wait_uses_durable_reservation_time_after_queue(self):
        class FixedClock:
            @staticmethod
            def now(_timezone):
                return self.now

            fromisoformat = staticmethod(datetime.fromisoformat)

        with fixture_database.connect(self.db) as conn:
            pre_pr_review.create(conn, "task", {"plan": review_plan()})
            entry = pre_pr_review.session(conn, task_id="task")
            run = pre_pr_review.new_run(conn, entry["round_id"], "review", "reviewer", "attempt", self.root, "prompt")
            created = (self.now - timedelta(seconds=5000)).isoformat()
            reserved = (self.now - timedelta(seconds=30)).isoformat()
            pre_pr_review.update_run(conn, run["run_id"], created_at=created, started_at=reserved)
        # The launch caller still holds the original pre-reservation record.
        stale_run = {**run, "created_at": created, "started_at": None}
        process = SimpleNamespace(pid=123456789, poll=Mock(side_effect=[None, 0]))
        driver = SimpleNamespace(
            store=pre_pr_review,
            db_path=self.db,
            round_id=entry["round_id"],
            processes={process.pid: process},
            check_active=Mock(),
            release_slot=Mock(),
            recovery_workers=lambda: {"reviewer": {"timeout_seconds": 60}},
            checkpoint_result=Mock(),
            poll_seconds=0,
        )
        with (
            patch.object(stages, "datetime", FixedClock),
            patch.object(stages.worker_identity, "terminate_recorded_identity") as terminate,
            patch.object(stages, "_wait_for_group_exit", return_value=True),
        ):
            stages.StageCoordinator._wait(driver, stale_run, {"pid": process.pid}, 60)
        terminate.assert_not_called()
        self.assertTrue(driver.release_slot.call_args.args[1]["process_exited"])
        with fixture_database.connect(self.db) as conn:
            saved = pre_pr_review.runs(conn, entry["round_id"])[0]
            self.assertEqual(saved["started_at"], reserved)
            self.assertTrue(saved["dispatch"]["process_exited"])

    def test_pre_pr_dispatch_command_receives_pipeline_envelope(self):
        from robert_agent.execution.budgets import pipeline_timeout

        plan = review_plan()
        prompt = self.root / "request.md"
        prompt.write_text("Implement the bounded change.\n")
        with fixture_database.connect(self.db) as conn:
            pre_pr_review.create(conn, "task", {"plan": plan, "implementer_bound": True})
            conn.execute("UPDATE attempts SET status='prepared' WHERE attempt_id='attempt'")
            conn.execute("UPDATE tasks SET lifecycle='queued' WHERE task_id='task'")
        with patch.object(
            dispatch.dispatch, "dispatch_worker", return_value={"ok": True, "status": "prepared"}
        ) as launch:
            result = dispatch.dispatch_queued(
                self.db,
                ["repo"],
                [
                    {
                        "task_info": {
                            "task_id": "task",
                            "attempt_id": "attempt",
                            "workstream_id": "stream",
                            "route_id": "new-pr",
                            "prompt_path": str(prompt),
                        },
                        "worktree_result": None,
                        "repo_id": "repo",
                        "repo": {"max_concurrency": 3},
                    }
                ],
                True,
                3,
                workers=[plan["implementer"]],
                default_worker=plan["implementer"],
            )
        self.assertEqual(launch.call_args.kwargs["worker_agent_config"]["timeout_seconds"], pipeline_timeout(plan))
        self.assertTrue(result[0]["model_coordinator"])

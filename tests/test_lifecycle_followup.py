"""Regressions for the October lifecycle audit, using production entrypoints."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import unittest
from unittest import mock

from tests import test_board_control_integration as board_fixture
from tests import test_lifecycle_remediation as lifecycle_fixture
from tests import test_paused_worker_supervision as paused_fixture
from tests import test_pending_handoff_recovery as handoff_fixture
from tests import test_run_once as run_fixture
from robert_agent import (
    board,
    daemon,
    daemon_state,
    loop_engine,
    result_spool,
    run_once,
    validate_config,
    work_items,
    worker_supervision,
)
from robert_agent.worker import result as worker_result


class LifecycleFollowupTests(unittest.TestCase):
    def fixture(self, cls):
        fixture = cls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def create(self, fixture, *, repo_id=None, key="create", **kwargs):
        repo_id = repo_id or fixture.repo_id
        context = work_items.CommandContext(
            "operator",
            "owner",
            frozenset({repo_id}),
            frozenset({"default", "reviewer"}),
        )
        return work_items.create_work_item(
            fixture.db_path,
            context=context,
            repo_id=repo_id,
            title="Analyze events",
            description="Explain event lifecycle",
            priority="P1",
            routing_mode=kwargs.get("routing_mode", "auto"),
            requested_worker=kwargs.get("requested_worker"),
            start=True,
            idempotency_key=key,
        )

    def test_normal_supervision_preserves_deferred_or_inflight_result(self):
        f = self.fixture(paused_fixture.PausedWorkerSupervisionTests)
        process = f.process(age=0)
        with closing(sqlite3.connect(f.db)) as conn:
            task, attempt = conn.execute(
                "SELECT task_id,attempt_id FROM attempts"
            ).fetchone()
        payload = {
            "task_id": task,
            "attempt_id": attempt,
            "output_type": "comment_analysis",
            "submission": {
                "planned_github_actions": [
                    {"type": "comment", "body": "Analysis finished"}
                ],
                "consumed_event_fingerprints": ["comment:first"],
                "verification": [],
                "handoff": "done",
                "used_skills": [],
            },
        }
        with mock.patch.object(
            worker_result,
            "_record_result_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            self.assertEqual(
                worker_result.record_result(f.db, payload)["status"], "deferred"
            )
        f.stop(process)
        with result_spool.locked_record(f.db, attempt):
            cycle = run_once.run_once(
                f.config_path,
                fixture_path=f.fixture.fixture.empty_fixture,
                dry_run=False,
                skip_external=True,
                skip_publish=True,
                max_dispatches=0,
            )
            self.assertTrue(cycle["ok"], cycle)
        with closing(sqlite3.connect(f.db)) as conn:
            self.assertNotEqual(
                conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed"
            )
        self.assertEqual(worker_result.replay_pending_results(f.db)["recorded"], 1)
        self.assertFalse(list(result_spool.directory(f.db).glob("rejected/*.json")))

    def test_repository_at_capacity_does_not_starve_other_repository(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        cfg = json.loads(f.config_path.read_text())
        cfg["max_concurrency"] = 3
        cfg["repos"][0]["max_concurrency"] = 1
        cfg["repos"].append(
            {**cfg["repos"][0], "full_name": "example/healthy", "max_concurrency": 2}
        )
        f.config_path.write_text(json.dumps(cfg))
        tasks = {}
        for repo, count in zip(cfg["repos"], (3, 1)):
            with closing(sqlite3.connect(f.db_path)) as conn, conn:
                rid = run_once._upsert_repo(conn, repo)
            tasks[rid] = [
                self.create(f, repo_id=rid, key=rid + str(i))["task_id"]
                for i in range(count)
            ]
        f._run_local_cycle()
        with closing(sqlite3.connect(f.db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='running' WHERE task_id=?",
                (tasks[f.repo_id][0],),
            )
        with (
            mock.patch.object(run_once, "_supervise_running_attempts", return_value=0),
            mock.patch.object(
                run_once, "_ensure_dispatch_worktree", side_effect=lambda d, r, t, w: w
            ),
            mock.patch.object(
                run_once.dispatch,
                "dispatch_worker",
                return_value={"ok": True, "status": "running"},
            ) as launch,
        ):
            cycle = run_once.run_once(
                f.config_path,
                dry_run=False,
                skip_external=True,
                skip_publish=True,
                consume_observations=True,
            )
        self.assertTrue(cycle["ok"], cycle)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(
            launch.call_args.kwargs["task_id"], tasks["repo:example/healthy"][0]
        )

    def test_ordinary_prepared_request_rechecks_current_trust(self):
        f = self.fixture(run_fixture.RunOnceDryRunTests)
        cycle = run_once.run_once(
            f.config_path,
            fixture_path=f.fixture_path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
        )
        f.config_path.write_text(
            f.config_path.read_text().replace("- wklken", "- replacement")
        )
        with (
            mock.patch.object(
                run_once, "_ensure_dispatch_worktree", side_effect=lambda d, r, t, w: w
            ),
            mock.patch.object(run_once.dispatch, "dispatch_worker") as launch,
        ):
            run_once.run_once(
                f.config_path,
                dry_run=False,
                skip_external=True,
                skip_publish=True,
                consume_observations=True,
            )
        launch.assert_not_called()
        with closing(sqlite3.connect(cycle["db_path"])) as conn:
            self.assertIn(
                conn.execute("SELECT status FROM attempts").fetchone()[0],
                ("failed", "canceled"),
            )
            self.assertFalse(
                loop_engine.has_runnable_local_work(
                    conn, repo_ids=["repo:example/backend"], include_publication=False
                )
            )

    def test_unavailable_manual_worker_stops_automatic_drain_until_retry(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        created = self.create(f, routing_mode="manual", requested_worker="reviewer")
        f._run_local_cycle()
        cfg = json.loads(f.config_path.read_text())
        cfg["workers"] = [cfg["workers"][0]]
        f.config_path.write_text(json.dumps(cfg))
        config = validate_config.validate_config(f.config_path, skip_external=True)
        with (
            mock.patch.object(
                run_once, "_ensure_dispatch_worktree", side_effect=lambda d, r, t, w: w
            ),
            mock.patch.object(run_once.dispatch, "dispatch_worker") as launch,
        ):
            run_once.run_once(
                f.config_path, dry_run=False, skip_external=True, skip_publish=True
            )
        launch.assert_not_called()
        with closing(sqlite3.connect(f.db_path)) as conn:
            self.assertFalse(
                loop_engine.has_runnable_local_work(
                    conn,
                    repo_ids=[f.repo_id],
                    config_result=config,
                    include_publication=False,
                )
            )
        card = board.get_work_item_detail(f.db_path, created["item"]["work_item_id"])
        self.assertIn("retry", card["valid_commands"])
        retry = work_items.execute_command(
            f.db_path,
            card["work_item_id"],
            context=f.context,
            command="retry",
            expected_version=card["version"],
            idempotency_key="retry-worker",
            routing_mode="auto",
        )
        self.assertTrue(retry["ok"])
        f._run_local_cycle()
        with closing(sqlite3.connect(f.db_path)) as conn:
            self.assertTrue(
                loop_engine.has_runnable_local_work(
                    conn,
                    repo_ids=[f.repo_id],
                    config_result=config,
                    include_publication=False,
                )
            )

    def test_paused_supervision_resolves_persisted_repository_id(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        now = run_once._now()
        with closing(sqlite3.connect(f.db_path)) as conn, conn:
            conn.execute("UPDATE repos SET repo_id='historic-id'")
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) VALUES('ws','historic-id','active','task',?,?)",
                (now, now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('task','ws','running',?,?)",
                (now, now),
            )
        with mock.patch.object(
            run_once, "_supervise_running_attempts", return_value=1
        ) as supervise:
            result = worker_supervision.run(f.config_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["supervised_count"], 1)
        self.assertEqual(supervise.call_args.args[1], "historic-id")

    def test_dead_worker_inspection_timeout_backs_off_and_stops(self):
        life = self.fixture(lifecycle_fixture.LifecycleRemediationTests)
        f, process, repo, now = life.running_fixture()
        life.stop_process(process)
        with mock.patch.object(
            run_once,
            "_inspect_attempt_recovery_with_metadata",
            side_effect=subprocess.TimeoutExpired(["git", "status"], 5),
        ) as inspect:
            for i in range(3):
                when = (
                    datetime.fromisoformat(now) + timedelta(seconds=20 * i)
                ).isoformat()
                with closing(sqlite3.connect(f.db)) as conn, conn:
                    run_once._supervise_running_attempts(conn, repo, {}, when)
                    if i < 2:
                        self.assertFalse(
                            loop_engine.has_runnable_local_work(
                                conn, repo_ids=[repo], now=when
                            )
                        )
            with closing(sqlite3.connect(f.db)) as conn, conn:
                run_once._supervise_running_attempts(
                    conn,
                    repo,
                    {},
                    (datetime.fromisoformat(now) + timedelta(hours=5)).isoformat(),
                )
                self.assertFalse(
                    loop_engine.has_runnable_local_work(conn, repo_ids=[repo])
                )
                self.assertEqual(run_once._running_attempt_count(conn, repo), 0)
                self.assertGreater(
                    conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 0
                )
        self.assertEqual(inspect.call_count, 3)

    def test_audit_inspection_timeout_stops_without_discarding_result(self):
        f = self.fixture(handoff_fixture.PendingHandoffRecoveryTests)
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            run_once.result_scope,
            "for_result",
            side_effect=subprocess.TimeoutExpired(["git", "rev-parse"], 5),
        ) as inspect:
            for i in range(3):
                when = (now + timedelta(seconds=20 * i)).isoformat()
                with mock.patch.object(run_once, "_now", return_value=when):
                    f.cycle(f.empty)
            f.cycle(f.empty)
        self.assertEqual(inspect.call_count, 3)
        with closing(sqlite3.connect(f.db)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 1
            )
            self.assertFalse(
                loop_engine.has_runnable_local_work(
                    conn, repo_ids=["repo:example/backend"], config_result=f.config
                )
            )
            item = conn.execute("SELECT work_item_id FROM work_items").fetchone()[0]
        card = board.get_work_item_detail(f.db, item)
        self.assertIn("retry", card["valid_commands"])
        with closing(sqlite3.connect(f.db)) as conn:
            before = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
        context = work_items.CommandContext(
            "operator",
            "owner",
            frozenset({"repo:example/backend"}),
            frozenset({"default"}),
        )
        retried = work_items.execute_command(
            f.db,
            item,
            context=context,
            command="retry",
            expected_version=card["version"],
            idempotency_key="retry-audit",
        )
        self.assertEqual(retried["task_id"], f.task)
        f.cycle(f.empty)
        with closing(sqlite3.connect(f.db)) as conn:
            self.assertIsNotNone(
                json.loads(
                    conn.execute("SELECT metadata_json FROM worker_results").fetchone()[
                        0
                    ]
                ).get("audit")
            )
            self.assertGreaterEqual(
                conn.execute("SELECT count(*) FROM attempts").fetchone()[0], before
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM attempts WHERE task_id=?", (f.task,)
                ).fetchone()[0],
                1,
            )

    def test_domain_failure_does_not_latch_control_lane(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        with closing(daemon_state.connect(f.db_path)) as conn, conn:
            owner = daemon_state.start_daemon_run(
                conn, config_path=str(f.config_path), owner_id="control"
            )["daemon_run_id"]
        context = daemon.DaemonContext(
            str(f.config_path),
            None,
            f.db_path,
            f.repo_id,
            2,
            dict(validate_config.DAEMON_DEFAULTS),
            owner,
            "lease",
        )
        failure = {
            "ok": False,
            "status": "partial_failure",
            "failure_scope": "repository",
            "repo_summaries": [{"status": "failed"}, {"status": "completed"}],
        }
        for _ in range(4):
            daemon._record_control_outcome(context, "local", failure)
        self.assertNotIn("local", context.control_attention)
        self.assertEqual(context.control_failures.get("local", 0), 0)

    def test_quota_deferral_never_consumes_control_failure_budget(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        with closing(daemon_state.connect(f.db_path)) as conn, conn:
            owner = daemon_state.start_daemon_run(
                conn, config_path=str(f.config_path), owner_id="quota"
            )["daemon_run_id"]
        context = daemon.DaemonContext(
            str(f.config_path),
            None,
            f.db_path,
            f.repo_id,
            2,
            dict(validate_config.DAEMON_DEFAULTS),
            owner,
            "lease",
        )
        for _ in range(4):
            daemon._record_control_outcome(
                context,
                "scan",
                {
                    "ok": False,
                    "status": "scanned",
                    "failures": [{"reason": "core_rate_limit_floor"}],
                    "retry_after_seconds": 60,
                },
            )
        self.assertNotIn("scan", context.control_attention)
        self.assertEqual(context.control_failures.get("scan", 0), 0)

    def test_local_preparation_failure_is_stopped_without_blocking_other_repo(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        config = json.loads(f.config_path.read_text())
        config["repos"].append({**config["repos"][0], "full_name": "example/healthy"})
        f.config_path.write_text(json.dumps(config))
        for repo in config["repos"]:
            with closing(sqlite3.connect(f.db_path)) as conn, conn:
                repo_id = run_once._upsert_repo(conn, repo)
            self.create(f, repo_id=repo_id, key=repo_id)
        original = run_once._prepare_worktree
        failures = []

        def prepare(repo, *args, **kwargs):
            if repo["full_name"] == "example/repo":
                failures.append(repo["full_name"])
                raise subprocess.CalledProcessError(
                    128, ["git", "fetch"], stderr="Permission denied"
                )
            return original(repo, *args, **kwargs)

        with mock.patch.object(run_once, "_prepare_worktree", side_effect=prepare):
            for _ in range(3):
                cycle = run_once.run_once(
                    f.config_path, dry_run=True, skip_external=True, skip_publish=True
                )
                self.assertTrue(cycle["ok"], cycle)
        self.assertEqual(len(failures), 1)
        with closing(sqlite3.connect(f.db_path)) as conn:
            states = dict(
                conn.execute(
                    "SELECT w.repo_id,t.lifecycle FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id"
                )
            )
            self.assertEqual(states[f.repo_id], "failed")
            self.assertEqual(states["repo:example/healthy"], "queued")
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM work_item_events WHERE event_type='execution_failed'"
                ).fetchone()[0],
                1,
            )

    def test_timeout_does_not_kill_worker_while_submission_lock_is_busy(self):
        f = self.fixture(paused_fixture.PausedWorkerSupervisionTests)
        process = f.process(age=120)
        with closing(sqlite3.connect(f.db)) as conn:
            attempt = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
        with result_spool.locked_record(f.db, attempt):
            with closing(sqlite3.connect(f.db)) as conn, conn:
                run_once._supervise_running_attempts(
                    conn, f.fixture.fixture.repo_id, f.config, f.now.isoformat()
                )
            self.assertIsNone(process.poll())
        f.stop(process)

    def test_repository_partial_failure_propagates_scope_through_local_loop(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        self.create(f)
        failed = {
            "ok": False,
            "status": "partial_failure",
            "failure_scope": "repository",
        }
        loop = loop_engine.run_loop(
            f.config_path,
            skip_external=True,
            skip_publish=True,
            runner=lambda *args, **kwargs: failed,
        )
        self.assertFalse(loop["ok"])
        self.assertEqual(loop["failure_scope"], "repository")
        self.assertEqual(loop["stop_reason"], "run_once_failed")

    def test_unexpected_pipeline_exception_retains_control_stop_budget(self):
        f = self.fixture(board_fixture.BoardControlIntegrationTests)
        self.create(f)
        with closing(daemon_state.connect(f.db_path)) as conn, conn:
            owner = daemon_state.start_daemon_run(conn, config_path=str(f.config_path), owner_id='exception')['daemon_run_id']
        context = daemon.DaemonContext(str(f.config_path), None, f.db_path, f.repo_id, 2,
            dict(validate_config.DAEMON_DEFAULTS), owner, 'lease')
        with mock.patch.object(run_once, '_audit_completed_results', side_effect=TypeError('controller invariant')):
            for _ in range(3):
                failed = run_once.run_once(f.config_path, fixture_path=f.empty_fixture,
                    dry_run=True, skip_external=True, skip_publish=True)
                self.assertEqual(failed['failure_scope'], 'controller')
                daemon._record_control_outcome(context, 'local', failed)
        self.assertIn('local', context.control_attention)

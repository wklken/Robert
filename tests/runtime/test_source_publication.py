"""Exercise publication composition without live provider calls."""

from tests import support  # noqa: F401

from unittest import mock

from robert_agent.application import publication_cycle
from robert_agent.delivery.application import lane, publish
from tests.support.daemon import DaemonFixture


class NativePublicationRuntimeTests(DaemonFixture):
    def test_explicit_zero_publication_budget_does_not_advance_issue_creation(self):
        for limit, expected_calls in ((-1, 0), (0, 0), (1, 1), (None, 1)):
            with (
                self.subTest(limit=limit),
                mock.patch.object(lane, "publish", return_value={"ok": True, "pending_count": 0}),
                mock.patch.object(publication_cycle.validation, "validate_config", return_value={"ok": True}),
                mock.patch.object(publication_cycle.dispatch_requests, "advance", return_value=0) as dispatch,
            ):
                publication_cycle.publish("/unused/config.yml", limit=limit)
                self.assertEqual(dispatch.call_count, expected_calls)

    def test_lane_preserves_native_work_and_shared_budget_on_github_core_floor(self):
        self._init_db()
        config_path = self._write_config()
        config = {
            "ok": True,
            "db_path": self.db_path,
            "tapd": {"enabled": True},
            "repos": [{"full_name": "example/repo", "github_account": "robot"}],
        }
        counters = {
            "ok": True,
            "status": "published",
            "pending_count": 1,
            "published_count": 1,
            "native_published_count": 1,
        }
        with (
            mock.patch.object(lane.validate_config, "validate_config", return_value=config),
            mock.patch.object(lane.github_read, "GitHubReadSession"),
            mock.patch.object(lane.github_budget, "budget", return_value=False),
            mock.patch.object(publish, "publish_ready_actions", return_value=counters) as publisher,
            mock.patch.object(lane, "source_ready", return_value=True),
            mock.patch.object(publication_cycle.dispatch_requests, "advance") as dispatch,
        ):
            result = publication_cycle.publish(config_path, limit=1, tapd_observation_provider=lambda: None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "core_rate_limit_floor")
        self.assertEqual((result["pending_count"], result["native_published_count"]), (1, 1))
        self.assertEqual([call.kwargs["provider"] for call in publisher.call_args_list], ["tapd"])
        dispatch.assert_not_called()

    def _publish_child(self, *, entrypoint, controller, lease_scope, lane_name="publish", spoof=False):
        from contextlib import closing
        import os
        import subprocess
        import sys
        import time

        from robert_agent.execution import runtime_state
        from robert_agent.runtime.control import ControlLane

        with closing(runtime_state.connect(self.db_path)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(
                conn, resource_key=lease_scope, owner_id=controller, ttl_seconds=60
            )
        self.assertTrue(lease["ok"], lease)
        command = """
import sys
from unittest.mock import patch
from robert_agent.delivery.application import lane, publish
from robert_agent.runtime import scan

db, entrypoint = sys.argv[1:]
config = {"ok": True, "db_path": db, "repos": [], "tapd": {"enabled": True}}

def inspect(db_path, **kwargs):
    return {
        "ok": True, "status": "published", "pending_count": 0,
        "source_owner": kwargs["source_owner"], "stop_set": kwargs["stop"].is_set(),
    }

with (
    patch.object(publish.supervision_publication, "fresh_config", return_value=config),
    patch.object(lane.validate_config, "validate_config", return_value=config),
    patch.object(lane, "source_ready", return_value=True),
    patch.object(publish, "publish_ready_actions", side_effect=inspect),
):
    if entrypoint == "publish":
        raise SystemExit(publish.main(["--db", db, "--config", "test.yml"]))
    raise SystemExit(scan.main(["--config", "test.yml", "--publish", "--source-only"]))
"""
        if spoof:
            from robert_agent.adapters.processes.identity import capture_process_identity
            from robert_agent.execution import control_children

            with closing(runtime_state.connect(self.db_path)) as conn, conn:
                control_children.record_starting(conn, "copied", controller, lane_name)
                control_children.claim_starting(conn, "copied", capture_process_identity(os.getpid()))
            completed = subprocess.run(
                [sys.executable, "-B", "-c", command, str(self.db_path), entrypoint],
                env={**os.environ, "_ROBERT_EXECUTOR_ID": "copied"},
                start_new_session=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 3, completed)
            import json

            return json.loads(completed.stdout)
        child = ControlLane(self.db_path, lane_name, controller)
        self.addCleanup(child.stop)
        child.start([sys.executable, "-B", "-c", command, str(self.db_path), entrypoint], timeout_seconds=12)
        deadline = time.monotonic() + 10
        result = None
        while result is None and time.monotonic() < deadline:
            result = child.poll()
            time.sleep(0.025)
        self.assertIsNotNone(result)
        return result

    def test_managed_scan_publisher_uses_actual_resident_owner(self):
        self._init_db()
        result = self._publish_child(
            entrypoint="scan", controller="resident", lease_scope=f"database:{self.db_path.resolve()}"
        )
        self.assertEqual(result["source_owner"], "resident", result)
        self.assertFalse(result["stop_set"])

    def test_direct_managed_publisher_uses_canonical_standalone_owner(self):
        self._init_db()
        scope = f"database:{self.db_path.resolve()}"
        result = self._publish_child(entrypoint="publish", controller="source-cli-" + "1" * 32, lease_scope=scope)
        self.assertEqual(result["source_owner"], f"source-cli:{scope}", result)
        self.assertFalse(result["stop_set"])

    def test_publisher_child_rejects_other_database_lease(self):
        self._init_db()
        result = self._publish_child(entrypoint="scan", controller="resident", lease_scope="database:/unrelated.sqlite")
        self.assertEqual(result["status"], "control_owner_required", result)

    def test_publisher_rejects_a_copied_executor_outside_the_managed_group(self):
        self._init_db()
        result = self._publish_child(
            entrypoint="publish", controller="resident", lease_scope=f"database:{self.db_path.resolve()}", spoof=True
        )
        self.assertEqual(result["status"], "control_owner_required", result)

    def test_deadline_precedes_new_standalone_claim_or_effect(self):
        from contextlib import closing
        import sqlite3

        self._init_db()
        config = {"tapd": {"enabled": True}}
        with (
            mock.patch.dict("os.environ", {"_ROBERT_CONTROL_DEADLINE": "0"}),
            mock.patch.object(lane, "source_ready", return_value=True),
            mock.patch.object(publish, "publish_ready_actions") as publisher,
        ):
            result = lane.publish_sources(self.db_path, config)
        publisher.assert_not_called()
        self.assertEqual(result["status"], "source_publication_stopped")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daemon_runs").fetchone(), (0,))

    def test_standalone_publication_does_not_steal_resident_lease_or_ignore_old_wrapper(self):
        from contextlib import closing

        from robert_agent.execution import control_children, runtime_state, source_controller

        self._init_db()
        scope = source_controller.resource_key(self.db_path)
        with closing(runtime_state.connect(self.db_path)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(conn, resource_key=scope, owner_id="resident", ttl_seconds=60)
        config = {"tapd": {"enabled": True}}
        with (
            mock.patch.object(lane, "source_ready", return_value=True),
            mock.patch.object(publish, "publish_ready_actions") as publisher,
        ):
            blocked = lane.publish_sources(self.db_path, config)
            self.assertEqual(blocked["status"], "skipped_active_daemon")
            with closing(runtime_state.connect(self.db_path)) as conn, conn:
                control_children.record_starting(conn, "unproved", "resident", "publish")
                runtime_state.release_daemon_lease(conn, lease["lease_id"])
            self.assertEqual(lane.publish_sources(self.db_path, config)["status"], "control_child_attention")
            publisher.assert_not_called()

    def test_resident_publisher_schedules_cold_completion_without_rescheduling_disabled_capability(self):
        from types import SimpleNamespace

        from robert_agent.runtime import daemon

        self._init_db()
        config_path = self._write_config()
        context, error = daemon._load_context(config_path)
        self.assertIsNone(error)
        context.next_live_poll_at = "2099-01-01T00:00:00+00:00"
        context.startup_live_poll_pending = False
        context.publisher_retry_at = float("inf")
        context.control_attention["local"] = {"status": "attention"}
        context.background_lanes = {
            name: SimpleNamespace(process=None, start=mock.Mock()) for name in ("scan", "publish")
        }
        with (
            mock.patch.object(lane, "source_ready", side_effect=[False, True]) as ready,
            mock.patch.object(daemon.loop_engine, "has_runnable_local_work", return_value=False),
            mock.patch.object(daemon, "_maybe_prune"),
        ):
            now = "2026-11-15T00:00:00+00:00"
            daemon._background_decision(context, now=now)
            context.background_lanes["publish"].start.assert_not_called()
            daemon._background_decision(context, now=now)
            context.background_lanes["publish"].start.assert_called_once()
            command = context.background_lanes["publish"].start.call_args.args[0]
            self.assertIn("--publish", command)
            self.assertIn("--source-only", command)
            self.assertIn("--source-reconcile-only", command)
        self.assertEqual(ready.call_args.kwargs["source_owner"], context.daemon_run_id)
        self.assertEqual(context.next_live_poll_at, "2099-01-01T00:00:00+00:00")

    def test_resident_and_oneshot_native_github_follow_github_budget_timer_and_pause_mode(self):
        from types import SimpleNamespace

        from robert_agent.runtime import daemon

        self._init_db()
        context, error = daemon._load_context(self._write_config())
        self.assertIsNone(error)
        context.runtime_config["tapd"] = {"enabled": True}
        context.next_live_poll_at = "2099-01-01T00:00:00+00:00"
        context.startup_live_poll_pending = False
        context.publisher_retry_at = float("inf")
        context.control_attention["local"] = {"status": "attention"}
        context.background_lanes = {
            name: SimpleNamespace(process=None, start=mock.Mock()) for name in ("scan", "publish")
        }

        def due(_conn, _config, **kwargs):
            if kwargs.get("provider") == "github":
                self.assertTrue(kwargs["reconcile_only"])
                self.assertFalse(kwargs["recover"])
                self.assertEqual(kwargs["source_owner"], context.daemon_run_id)
                return True
            return False

        with (
            mock.patch.object(lane, "source_ready", side_effect=due),
            mock.patch.object(daemon.loop_engine, "has_runnable_local_work", return_value=False),
            mock.patch.object(daemon, "_maybe_prune"),
            mock.patch("robert_agent.runtime.control.recover", return_value=True),
            mock.patch.object(daemon.native_sources, "run_control_child", return_value={"ok": True}) as run,
        ):
            now = "2026-11-15T00:00:00+00:00"
            daemon._background_decision(context, now=now)
            context.background_lanes["publish"].start.assert_not_called()
            context.publisher_retry_at = 0
            daemon._background_decision(context, now=now)
            context.background_lanes["publish"].start.assert_called_once()
            command = context.background_lanes["publish"].start.call_args.args[0]
            self.assertNotIn("--source-only", command)
            self.assertIn("--source-reconcile-only", command)
            self.assertEqual(daemon._publish_native_once(context, local_only=True), {"ok": True})
            self.assertEqual(run.call_args.args[:3], (context.db_path, "publish", context.daemon_run_id))
            self.assertIn("--native-only", run.call_args.args[3])
            self.assertIn("--source-reconcile-only", run.call_args.args[3])

    def test_legacy_repo_fallback_keeps_upstream_stop_without_native_owner(self):
        from threading import Event

        self._init_db()
        stop = Event()
        config = {"tapd": {"enabled": True}}
        with (
            mock.patch.object(lane, "source_ready", return_value=False),
            mock.patch.object(publish, "publish_ready_actions", return_value={"ok": True}) as publisher,
        ):
            self.assertTrue(lane.publish_github_repo(self.db_path, config, "repo", stop=stop)["ok"])
        self.assertIs(publisher.call_args.kwargs["stop"], stop)
        self.assertIsNone(publisher.call_args.kwargs.get("source_owner"))

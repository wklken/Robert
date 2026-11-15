from tests import support  # noqa: F401

from contextlib import closing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from robert_agent.execution import runtime_state
from robert_agent.runtime import control, daemon, native_sources
from tests.support.daemon import DaemonFixture


class NativeSchedulingTests(unittest.TestCase):
    def test_source_completion_preserves_github_poll_and_domain_attention_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            context = daemon.DaemonContext(
                "config", None, Path(directory) / "db", "repo:o/r", 1, {"github_poll_seconds": 900}, "owner", "lease"
            )
            now = "2026-07-01T00:00:00+00:00"
            context.next_live_poll_at = "2026-07-01T00:15:00+00:00"
            context.background_lanes = {
                "native_scan": mock.Mock(**{"poll.return_value": {"ok": True, "status": "observed"}}),
                "native_model": mock.Mock(**{"poll.return_value": {"ok": True, "status": "attention"}}),
            }
            with (
                mock.patch.object(daemon.daemon_state, "connect"),
                mock.patch.object(daemon.daemon_state, "record_event"),
                mock.patch.object(daemon, "_record_control_outcome") as outcome,
            ):
                daemon._poll_background_lanes(context, now=now)
            self.assertEqual(context.next_live_poll_at, "2026-07-01T00:15:00+00:00")
            self.assertEqual(context.next_native_scan_at, "2026-07-01T00:00:30+00:00")
            outcome.assert_any_call(context, "native_model", {"ok": True, "status": "attention"}, now=now)

    def test_scan_deadline_and_running_lane_bound_idle_wait(self):
        context = daemon.DaemonContext(
            "config", None, Path("unused"), "repo:o/r", 1, {"local_poll_seconds": 70}, "owner", "lease"
        )
        context.next_native_scan_at = "2026-07-01T00:00:30+00:00"
        now = "2026-07-01T00:00:00+00:00"
        context.background_lanes = {"native_scan": mock.Mock(process=None)}
        self.assertEqual(daemon._idle_sleep_seconds(context, now=now), 30)
        context.background_lanes["native_model"] = mock.Mock(process=object())
        self.assertEqual(daemon._idle_sleep_seconds(context, now=now), 5)


class NativeChildTests(DaemonFixture):
    def _run_owner_validation_child(self, *, source_owner, lease_scope, controller="resident"):
        self._init_db()
        config_path = self._write_config()
        with closing(runtime_state.connect(self.db_path)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(
                conn, resource_key=lease_scope, owner_id=controller, ttl_seconds=60
            )
        self.assertTrue(lease["ok"])
        lane = control.ControlLane(self.db_path, "native_scan", controller)
        self.addCleanup(lane.stop)
        lane.start(
            native_sources.command("native_scan", config_path, self.db_path, source_owner),
            timeout_seconds=10,
        )
        result = None
        deadline = time.monotonic() + 10
        while result is None and time.monotonic() < deadline:
            result = lane.poll()
            if result is None:
                time.sleep(0.025)
        self.assertIsNotNone(result)
        return result

    def test_native_child_rejects_unrelated_supplied_source_owner(self):
        result = self._run_owner_validation_child(
            source_owner="another-source-owner", lease_scope=f"database:{self.db_path.resolve()}"
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "control_owner_required")

    def test_native_child_rejects_lease_for_a_different_database(self):
        result = self._run_owner_validation_child(
            source_owner="resident", lease_scope=f"database:{self.db_path.resolve()}.other"
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "control_owner_required")

    def test_standalone_child_accepts_its_canonical_source_owner(self):
        scope = f"database:{self.db_path.resolve()}"
        result = self._run_owner_validation_child(
            source_owner=f"source-cli:{scope}", lease_scope=scope, controller="source-cli-" + "a" * 32
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "disabled")

    def test_resident_child_cannot_borrow_standalone_source_owner(self):
        scope = f"database:{self.db_path.resolve()}"
        result = self._run_owner_validation_child(source_owner=f"source-cli:{scope}", lease_scope=scope)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "control_owner_required")

    def test_disabled_scan_control_child_runs_while_model_and_local_lane_have_independent_lifetimes(self):
        self._init_db()
        config_path = self._write_config()
        scope = f"database:{self.db_path.resolve()}"
        with closing(runtime_state.connect(self.db_path)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(conn, resource_key=scope, owner_id="owner", ttl_seconds=60)
        self.assertTrue(lease["ok"])
        config = {"db_path": str(self.db_path)}
        lanes = {
            name: control.ControlLane(self.db_path, name, "owner") for name in ("native_model", "native_scan", "local")
        }
        for lane in lanes.values():
            self.addCleanup(lane.stop)
        lanes["native_model"].start([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=20)
        lanes["native_scan"].start(
            native_sources.command("native_scan", config_path, self.db_path, "owner"),
            timeout_seconds=native_sources.timeout("native_scan", config),
        )
        lanes["local"].start([sys.executable, "-c", """print('{"ok":true,"status":"done"}')"""], timeout_seconds=10)
        results = {}
        deadline = time.monotonic() + 10
        while len(results) < 2 and time.monotonic() < deadline:
            for name in ("native_scan", "local"):
                if name not in results and (result := lanes[name].poll()) is not None:
                    results[name] = result
            time.sleep(0.025)
        self.assertTrue(results["native_scan"]["ok"], results)
        self.assertEqual(results["native_scan"]["status"], "disabled")
        self.assertEqual(results["local"]["status"], "done")
        self.assertIsNone(lanes["native_model"].process.poll())

    def test_standalone_source_does_not_override_a_resident_controller(self):
        self._init_db()
        with closing(runtime_state.connect(self.db_path)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(
                conn, resource_key=f"database:{self.db_path.resolve()}", owner_id="resident", ttl_seconds=60
            )
        self.assertTrue(lease["ok"])
        with (
            mock.patch.object(native_sources, "runnable", return_value=True),
            mock.patch.object(native_sources, "_run_bounded") as step,
        ):
            result = native_sources.run_once("config", {"db_path": self.db_path})
        self.assertEqual(result, {"status": "skipped_active_daemon"})
        step.assert_not_called()

    def test_one_shot_failed_child_remains_visible_after_repository_success(self):
        from robert_agent.runtime import cycle

        self._init_db()
        config_path = self._write_config()
        native = {"native_model": {"ok": False, "status": "child_timeout"}}
        with (
            mock.patch.object(native_sources, "runnable", return_value=True),
            mock.patch.object(native_sources, "run_once", return_value=native) as run_native,
            mock.patch.object(cycle.readiness, "has_local_work", return_value=False),
        ):
            result = cycle.run_once(config_path, dry_run=False, skip_external=True, consume_observations=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_source_control")
        self.assertEqual(result["native_sources"], native)
        self.assertTrue(run_native.call_args.kwargs["local_only"])

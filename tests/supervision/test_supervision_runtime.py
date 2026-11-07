from robert_agent.supervision import health as supervision_health
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.runtime import daemon
from robert_agent.execution import runtime_state as daemon_state
from robert_agent.adapters.sqlite import database as storage


class RuntimeTickTests(unittest.TestCase):
    def test_legacy_alert_owner_returns_if_supervision_coverage_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "db_path": str(Path(directory) / "state"),
                "data_dir": directory,
                "supervision": {"mode": "observe"},
            }
            health = {
                "ok": True,
                "checked_at": "2026-11-06T10:00:00+00:00",
                "last_complete_check_at": "2026-11-06T10:00:00+00:00",
            }
            supervision_health.atomic_json(
                supervision_health.health_path(config), health
            )
            self.assertTrue(
                supervision_health.owns_health_alerts(
                    config, "2026-11-06T10:00:01+00:00"
                )
            )
            self.assertFalse(
                supervision_health.owns_health_alerts(
                    config, "2026-11-06T11:00:00+00:00"
                )
            )
            config["supervision"]["mode"] = "off"
            self.assertFalse(
                supervision_health.owns_health_alerts(
                    config, "2026-11-06T10:00:01+00:00"
                )
            )

    def test_daemon_tick_survives_outside_database_and_preserves_start_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.db"
            storage.init_database(db)
            config = {"data_dir": directory, "db_path": str(db), "supervision": {}}
            with closing(sqlite3.connect(db)) as conn, conn:
                daemon_state.start_daemon_run(
                    conn,
                    config_path="/fixture",
                    owner_id="run",
                    now="2026-11-06T10:00:00+00:00",
                )
                lease = daemon_state.acquire_daemon_lease(
                    conn,
                    resource_key="database:" + str(db),
                    owner_id="run",
                    ttl_seconds=60,
                )
            context = daemon.DaemonContext(
                config_path="/fixture",
                workflow_path=None,
                db_path=db,
                repo_id="r",
                max_concurrency=1,
                daemon_config={
                    "local_poll_seconds": 5,
                    "live_run_timeout_seconds": 30,
                    "local_drain_timeout_seconds": 30,
                },
                daemon_run_id="run",
                lease_id=lease["lease_id"],
                runtime_config=config,
            )
            daemon._heartbeat(context, now="2026-11-06T10:00:01+00:00")
            path = supervision_health.runtime_dir(config) / "daemon.json"
            first = json.loads(path.read_text())
            daemon._heartbeat(context, now="2026-11-06T10:00:02+00:00")
            second = json.loads(path.read_text())
            self.assertEqual(first["started_at"], second["started_at"])
            self.assertNotEqual(first["observed_at"], second["observed_at"])
            self.assertEqual(second["build_status"], "unverified")
            daemon._finish_context(context, "stopped", now="2026-11-06T10:00:03+00:00")
            self.assertEqual(json.loads(path.read_text())["status"], "stopped")

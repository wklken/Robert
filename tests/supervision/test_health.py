"""Database outage notices survive intermittent recovery and process restarts."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from robert_agent.persistence.schema import database as storage
from robert_agent.supervision import health
from robert_agent.supervision.application import checks


class HealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "state.db"
        storage.init_database(self.db)
        self.config = {"db_path": str(self.db), "data_dir": str(self.root), "supervision": {"poll_seconds": 60}}
        self.sent = []
        notifier = patch.dict("os.environ", {"ROBERT_NOTIFICATION_COMMAND": "fixture"})
        notifier.start()
        self.addCleanup(notifier.stop)

    def record(self, minute, ok, *, second=0, reason="database_busy", deliver=True):
        now = f"2026-11-20T00:{minute:02}:{second:02}+00:00"
        result = {"ok": ok, "status": "observed" if ok else "unobservable", "reason_code": None if ok else reason}
        health.health(dict(self.config), result, now)
        if deliver:
            checks.deliver_health(self.db, health.health_path(self.config), runner=self.send, config=self.config)
        return health.read_health(self.config)

    def send(self, argv, **kwargs):
        self.sent.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0)

    def test_brief_recovery_keeps_notice_identity_and_budget(self):
        first = self.record(0, False)
        self.record(1, False)
        self.record(2, True)
        self.record(3, False, reason="database_observation_deadline")
        latest = self.record(4, False)
        self.assertEqual(latest["delivery_id"], first["delivery_id"])
        self.assertEqual(len(self.sent), 1)
        with closing(sqlite3.connect(self.db)) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM notifications").fetchone()[0])
        self.assertEqual(metadata["reason_code"], "database_busy")

    def test_isolated_failures_remain_silent_and_stale_failure_cannot_send(self):
        for minute in range(6):
            state = self.record(minute, minute % 2 == 1)
        self.assertTrue(state["ok"])
        self.assertEqual(self.sent, [])
        self.record(6, False)
        checks.deliver_health(self.db, health.health_path(self.config), runner=self.send, config=self.config)
        self.assertEqual(self.sent, [])
        # Two observations must still span the polling interval after a brief success.
        self.record(6, False, second=1)
        self.assertEqual(self.sent, [])
        self.record(7, False)
        self.assertEqual(len(self.sent), 1)

    def test_stable_recovery_allows_a_new_outage(self):
        first = self.record(0, False)
        self.record(1, False)
        for minute in range(2, 7):
            state = self.record(minute, True)
            self.assertEqual(state["delivery_id"], first["delivery_id"])
        state = self.record(7, True)
        self.assertNotIn("delivery_id", state)
        self.assertNotIn("first_failed_at", state)
        self.record(8, False)
        latest = self.record(9, False)
        self.assertNotEqual(latest["delivery_id"], first["delivery_id"])
        self.assertEqual(len(self.sent), 2)

    def test_unsampled_time_does_not_prove_stable_recovery(self):
        first = self.record(0, False)
        self.record(1, False)
        self.record(2, True)
        self.record(10, True)
        self.record(11, False)
        state = self.record(12, False)
        self.assertEqual(state["delivery_id"], first["delivery_id"])
        self.assertEqual(len(self.sent), 1)

    def test_restart_during_brief_recovery_preserves_delivered_notice(self):
        first = self.record(0, False)
        self.record(1, False)
        self.record(2, True)
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; from robert_agent.supervision import health; "
                "health.health(json.loads(sys.argv[1]), {'ok':False,'status':'unobservable','reason_code':'database_busy'}, "
                "'2026-11-20T00:03:00+00:00')",
                json.dumps(self.config),
            ],
            check=True,
            timeout=5,
            capture_output=True,
        )
        latest = self.record(4, False)
        self.assertEqual(latest["delivery_id"], first["delivery_id"])
        self.assertEqual(len(self.sent), 1)

    def test_activation_cutoff_is_preserved_during_brief_recovery(self):
        self.config["messaging"] = {"enabled": False, "send_after": "2026-11-20T00:02:00+00:00"}
        self.record(0, False)
        self.record(1, False)
        self.record(2, True)
        self.record(3, False)
        self.record(4, False)
        self.assertEqual(self.sent, [])

    def test_failed_transport_budget_survives_brief_recovery(self):
        with patch.object(checks.message_transport, "send", return_value=False) as sender:
            self.record(0, False)
            for minute in range(1, 5):
                self.record(minute, False)
            self.record(5, True)
            self.record(6, False)
            self.record(7, False)
        self.assertEqual(sender.call_count, 3)

    def test_failure_diagnostics_remain_available_during_recovery(self):
        diagnostic = {"phase": "record", "sqlite_errorname": "SQLITE_BUSY", "elapsed_seconds": 1.1}
        health.health(
            self.config,
            {"ok": False, "status": "unobservable", "reason_code": "database_busy", "diagnostics": diagnostic},
            "2026-11-20T00:00:00+00:00",
        )
        state = self.record(1, True, deliver=False)
        self.assertTrue(state["ok"])
        self.assertEqual(state["last_failure"]["diagnostics"], diagnostic)

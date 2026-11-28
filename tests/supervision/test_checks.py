"""Recorded observations tolerate short writers and explain bounded failures."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from robert_agent.persistence.schema import database as storage
from robert_agent.supervision import health
from robert_agent.supervision.application import checks


class CheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "state.db"
        storage.init_database(self.db)
        self.config = {"db_path": str(self.db), "data_dir": temporary.name, "supervision": {"poll_seconds": 60}}

    def test_status_distinguishes_current_and_stale_observations(self):
        sample = {
            "ok": True,
            "status": "observed",
            "checked_at": "2026-11-20T00:00:00+00:00",
            "last_complete_check_at": "2026-11-20T00:00:00+00:00",
        }
        health.atomic_json(health.health_path(self.config), sample)
        for now, freshness, age in (
            ("2026-11-20T00:02:10+00:00", "fresh", 130),
            ("2026-11-20T00:02:11+00:00", "stale", 131),
            ("2026-11-20T00:11:00+00:00", "stale", 660),
        ):
            with self.subTest(now=now), patch.object(health, "utc_now", return_value=now):
                result = checks.status(self.config)
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["health"]["freshness"], freshness)
                self.assertEqual(result["health"]["observation_age_seconds"], age)
                self.assertEqual(result["health"]["status"], "observed")
        self.assertEqual(health.read_health(self.config), sample)

    def test_status_exposes_stale_complete_coverage_despite_fresh_tick(self):
        health.atomic_json(
            health.health_path(self.config),
            {
                "ok": True,
                "status": "observed",
                "checked_at": "2026-11-20T00:11:00+00:00",
                "last_complete_check_at": "2026-11-20T00:00:00+00:00",
            },
        )
        with patch.object(health, "utc_now", return_value="2026-11-20T00:11:01+00:00"):
            result = checks.status(self.config)["health"]
        self.assertEqual(result["freshness"], "fresh")
        self.assertEqual(result["coverage_freshness"], "stale")
        self.assertEqual(result["complete_observation_age_seconds"], 661)
        self.assertFalse(health.owns_health_alerts(self.config, "2026-11-20T00:11:01+00:00"))

    def test_status_marks_missing_invalid_and_future_observations_unknown(self):
        for sample in (
            None,
            [],
            "invalid sample",
            {"ok": True, "status": "observed", "checked_at": "invalid"},
            {"ok": True, "status": "observed", "checked_at": "2026-11-21T00:00:00+00:00"},
        ):
            with self.subTest(sample=sample):
                if sample is not None:
                    health.atomic_json(health.health_path(self.config), sample)
                with patch.object(health, "utc_now", return_value="2026-11-20T00:00:00+00:00"):
                    result = checks.status(self.config)["health"]
                self.assertEqual(result["freshness"], "unknown")
                self.assertIsNone(result["observation_age_seconds"])
                self.assertEqual(result["coverage_freshness"], "unknown")

    def test_short_external_writer_is_waited_out(self):
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); "
                "c.execute('BEGIN IMMEDIATE'); print('locked',flush=True); time.sleep(.4); c.rollback()",
                str(self.db),
            ],
            stdout=subprocess.PIPE,
            text=True,
        ) as writer:
            self.assertEqual(writer.stdout.readline().strip(), "locked")
            result = checks.check(self.config, record=True)
            writer.wait(timeout=3)
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM supervision_checkpoints WHERE scope_id='supervisor'").fetchone()[0],
                1,
            )

    def test_sustained_writer_reports_record_phase_and_does_not_claim_read_failure(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = checks.check(self.config, record=True)
            elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "database_busy")
        self.assertEqual(result["diagnostics"]["phase"], "record")
        self.assertEqual(result["diagnostics"]["sqlite_errorname"], "SQLITE_BUSY")
        self.assertLess(elapsed, 3)
        self.assertIn("写入", result["safe_error"])
        self.assertNotIn("持续", result["safe_error"])
        self.assertEqual(health.read_health(self.config)["diagnostics"], result["diagnostics"])

    def test_read_only_check_does_not_wait_for_an_external_writer(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            result = checks.check(self.config)
        self.assertTrue(result["ok"], result)
        self.assertFalse(health.health_path(self.config).exists())

    def test_observation_is_revalidated_after_waiting_for_writer(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','default','main','/tmp','/tmp')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES('stream','repo','task','old','old')"
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('task','stream','running','old','old')"
            )
        observation = {
            "scope_type": "task",
            "scope_id": "task",
            "rule_id": "execution_failed",
            "classification": "confirmed",
            "reason_code": "execution_failed",
            "severity": "error",
            "evidence": {},
            "expected_progress": {},
            "generation": {
                "lifecycle": "running",
                "task_updated_at": "old",
                "attempt_id": None,
                "attempt_status": None,
                "result_id": None,
                "audit_status": None,
            },
        }
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); "
                "c.execute('BEGIN IMMEDIATE'); c.execute(\"UPDATE tasks SET updated_at='new' WHERE task_id='task'\"); "
                "print('locked',flush=True); time.sleep(.4); c.commit()",
                str(self.db),
            ],
            stdout=subprocess.PIPE,
            text=True,
        ) as writer:
            self.assertEqual(writer.stdout.readline().strip(), "locked")
            with patch.object(checks.supervision_observe, "collect", return_value={"observations": [observation]}):
                result = checks.check(self.config, record=True)
            writer.wait(timeout=3)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["observations"], [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM supervision_incidents").fetchone()[0], 0)

    def test_observation_failure_preserves_safe_underlying_error_name(self):
        from robert_agent.supervision.application.observation import ObservationUnavailable

        cause = sqlite3.OperationalError("private SQL and input must not be recorded")
        cause.sqlite_errorname = "SQLITE_INTERRUPT"
        unavailable = ObservationUnavailable("database_observation_deadline")
        unavailable.__cause__ = cause
        with patch.object(checks.supervision_observe, "collect", side_effect=unavailable):
            result = checks.check(self.config, record=True)
        self.assertEqual(result["diagnostics"]["phase"], "collect")
        self.assertEqual(result["diagnostics"]["sqlite_errorname"], "SQLITE_INTERRUPT")
        self.assertNotIn("private SQL", str(health.read_health(self.config)))

    def test_missing_database_identifies_schema_read_phase(self):
        self.config["db_path"] = str(self.db.with_name("missing.db"))
        result = checks.check(self.config, record=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["diagnostics"]["phase"], "schema")
        self.assertFalse(Path(self.config["db_path"]).exists())

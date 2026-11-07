from contextlib import closing
from datetime import datetime
import sqlite3
from unittest import mock
from pathlib import Path
from tests.support.daemon import DaemonFixture


class DaemonSchedulingTests(DaemonFixture):
    def test_resident_loop_waits_on_listener_and_closes_it(self):
        from robert_agent.runtime import daemon

        config_path = self._write_config()
        self._init_db()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
        context, error = daemon._load_context(
            config_path,
            now="2026-07-03T00:00:00+00:00",
        )
        self.assertIsNone(error)
        calls = []

        class Listener:
            def __init__(self, db_path):
                calls.append(("open", Path(db_path)))

            def wait(self, timeout_seconds):
                calls.append(("wait", timeout_seconds))
                daemon._STOP_REQUESTED = True
                return True

            def close(self):
                calls.append(("close", None))

        with (
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(
                daemon,
                "run_once_decision",
                return_value={"ok": True, "decision": "idle"},
            ),
        ):
            result = daemon._resident_loop(context, listener_factory=Listener)

        with closing(sqlite3.connect(self.db_path)) as conn:
            event_types = {
                row[0]
                for row in conn.execute("SELECT event_type FROM daemon_events")
            }
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            calls,
            [("open", self.db_path), ("wait", 5), ("close", None)],
        )
        self.assertIn("wakeup_listener_started", event_types)

    def test_resident_loop_falls_back_to_polling_when_listener_is_unavailable(self):
        from robert_agent.runtime import daemon

        config_path = self._write_config()
        self._init_db()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
        context, error = daemon._load_context(
            config_path,
            now="2026-07-03T00:00:00+00:00",
        )
        self.assertIsNone(error)
        sleeps = []

        def unavailable_listener(_db_path):
            raise OSError("socket path too long")

        def sleeper(seconds):
            sleeps.append(seconds)
            daemon._STOP_REQUESTED = True

        with (
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(
                daemon,
                "run_once_decision",
                return_value={"ok": True, "decision": "idle"},
            ),
        ):
            result = daemon._resident_loop(
                context,
                listener_factory=unavailable_listener,
                sleeper=sleeper,
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            event_types = {
                row[0]
                for row in conn.execute("SELECT event_type FROM daemon_events")
            }
        self.assertTrue(result["ok"], result)
        self.assertEqual(sleeps, [5])
        self.assertIn("wakeup_listener_unavailable", event_types)

    def test_resident_loop_closes_listener_when_start_event_fails(self):
        from robert_agent.runtime import daemon

        config_path = self._write_config()
        self._init_db()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
        context, error = daemon._load_context(
            config_path,
            now="2026-07-03T00:00:00+00:00",
        )
        self.assertIsNone(error)
        closed = []

        class Listener:
            path = Path("/tmp/daemon-wakeup.sock")

            def __init__(self, _db_path):
                pass

            def close(self):
                closed.append(True)

        with (
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(
                daemon.daemon_state,
                "record_event",
                side_effect=RuntimeError("event write failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "event write failed"):
                daemon._resident_loop(context, listener_factory=Listener)

        self.assertEqual(closed, [True])

    def test_default_lease_ttl_covers_live_and_local_decision_window(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config()
        now = "2026-07-03T00:00:00+00:00"
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)

        with closing(sqlite3.connect(self.db_path)) as conn:
            expires_at = conn.execute(
                "SELECT expires_at FROM leases WHERE lease_id = ?",
                (context.lease_id,),
            ).fetchone()[0]

        expires_dt = datetime.fromisoformat(expires_at)
        now_dt = datetime.fromisoformat(now)
        self.assertGreaterEqual((expires_dt - now_dt).total_seconds(), 510)

    def test_load_context_uses_existing_repo_ids_for_configured_repos(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config()
        now = "2026-07-03T00:00:00+00:00"
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)
        self.assertEqual(context.repo_id, "repo-1")
        self.assertEqual(context.repo_ids, ["repo-1"])

    def test_run_once_decision_only_uses_run_on_start_once_before_next_poll(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config({"daemon": {"scan_on_start": True}})
        now = "2026-07-03T00:00:00+00:00"
        calls = []
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":5000},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        first = daemon.run_once_decision(
            context,
            now=now,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )
        second = daemon.run_once_decision(
            context,
            now="2026-07-03T00:01:00+00:00",
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )

        self.assertEqual(first["decision"], "live_poll")
        self.assertEqual(second["decision"], "idle")
        self.assertEqual(len(calls), 2)

    def test_run_once_decision_full_capacity_uses_when_full_interval(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config({"scheduler": {"max_concurrency": 1}})
        now = "2026-07-03T00:00:00+00:00"
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at)
                VALUES ('task-1', 'ws-1', 'running', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (now, now),
            )

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)

        with mock.patch.object(daemon.loop_engine, "has_runnable_local_work", return_value=False):
            result = daemon.run_once_decision(context, now=now)

        self.assertEqual(result["decision"], "idle")
        self.assertEqual(
            context.next_live_poll_at,
            "2026-07-03T00:10:00+00:00",
        )

    def test_run_once_decision_running_attempt_initializes_and_honors_live_poll_schedule(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config(
            {"scheduler": {"max_concurrency": 3}, "github": {"account": "robot", "polling": {"fallback_seconds": 300}}}
        )
        now = "2026-07-03T00:00:00+00:00"
        calls = []
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at)
                VALUES ('task-1', 'ws-1', 'running', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (now, now),
            )

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)
        self.assertIsNone(context.next_live_poll_at)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_object('result_contract','compact-v1') WHERE attempt_id='attempt-1'")

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":5000},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        first = daemon.run_once_decision(
            context,
            now=now,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )
        second = daemon.run_once_decision(
            context,
            now="2026-07-03T00:05:00+00:00",
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )

        self.assertEqual(first["decision"], "local_drain")
        self.assertEqual(second["decision"], "live_poll")
        self.assertEqual(context.next_live_poll_at, "2026-07-03T00:10:00+00:00")
        self.assertEqual(len(calls), 3)
        self.assertIn("runtime/loop.py", " ".join(calls[0]))
        self.assertIn("runtime/cycle.py", " ".join(calls[1]))
        self.assertIn("runtime/loop.py", " ".join(calls[2]))

    def test_run_once_decision_startup_live_poll_runs_at_full_capacity(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config(
            {"scheduler": {"max_concurrency": 1}, "daemon": {"scan_on_start": True}, "github": {"account": "robot", "polling": {"synchronous": {"full_capacity_seconds": 300}}}}
        )
        now = "2026-07-03T00:00:00+00:00"
        calls = []
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at)
                VALUES ('task-1', 'ws-1', 'running', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (now, now),
            )

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":5000},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        first = daemon.run_once_decision(
            context,
            now=now,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status = 'completed' WHERE attempt_id = 'attempt-1'")
        second = daemon.run_once_decision(
            context,
            now="2026-07-03T00:01:00+00:00",
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )

        self.assertEqual(first["decision"], "live_poll")
        self.assertEqual(second["decision"], "idle")
        self.assertFalse(context.startup_live_poll_pending)
        self.assertEqual(context.next_live_poll_at, "2026-07-03T00:05:00+00:00")
        self.assertEqual(len(calls), 2)
        self.assertIn("runtime/cycle.py", " ".join(calls[0]))
        self.assertIn("runtime/loop.py", " ".join(calls[1]))

    def test_run_once_decision_due_live_poll_is_not_starved_by_running_attempt(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config(
            {"scheduler": {"max_concurrency": 1}, "github": {"account": "robot", "polling": {"synchronous": {"full_capacity_seconds": 300}}}}
        )
        now = "2026-07-03T00:05:00+00:00"
        calls = []
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-1', 'repo-1', 'active', 'task-1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at)
                VALUES ('task-1', 'ws-1', 'running', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-1', 'task-1', 1, 'running', ?, ?)
                """,
                (now, now),
            )

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)
        context.next_live_poll_at = now

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":5000},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        result = daemon.run_once_decision(
            context,
            now=now,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
        )

        self.assertEqual(result["decision"], "live_poll")
        self.assertEqual(context.next_live_poll_at, "2026-07-03T00:10:00+00:00")
        self.assertEqual(len(calls), 2)
        self.assertIn("runtime/cycle.py", " ".join(calls[0]))
        self.assertIn("runtime/loop.py", " ".join(calls[1]))

    def test_once_runs_local_drain_for_due_wakeup_without_live_poll(self):
        from robert_agent.runtime import daemon
        from robert_agent.adapters.sqlite import database as storage

        config_path = self._write_config()
        storage.init_database(self.db_path)
        now = "2026-07-03T00:00:00+00:00"
        calls = []

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
            conn.execute(
                """
                INSERT INTO wakeups(
                  wakeup_id, repo_id, reason, dedupe_key, status,
                  not_before_at, created_at, updated_at
                )
                VALUES ('wakeup-1', 'repo-1', 'manual_operator_request', 'manual-1',
                        'pending', ?, ?, ?)
                """,
                (now, now, now),
            )

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        result = daemon.run_daemon(
            config_path,
            workflow_path="src/robert_agent/references/workflow.yml",
            once=True,
            child_runner=child_runner,
            now=now,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["decision"], "local_drain")
        self.assertEqual(len(calls), 1)
        self.assertIn("runtime/loop.py", " ".join(calls[0]))
        self.assertNotIn("runtime/cycle.py", " ".join(calls[0]))

    def test_once_runs_local_drain_with_skip_publish_when_rate_limit_floor_is_hit(self):
        from robert_agent.runtime import daemon
        from robert_agent.adapters.sqlite import database as storage

        config_path = self._write_config()
        storage.init_database(self.db_path)
        now = "2026-07-03T00:00:00+00:00"
        calls = []

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)
            conn.execute(
                """
                INSERT INTO wakeups(
                  wakeup_id, repo_id, reason, dedupe_key, status,
                  not_before_at, created_at, updated_at
                )
                VALUES ('wakeup-1', 'repo-1', 'manual_operator_request', 'manual-1',
                        'pending', ?, ?, ?)
                """,
                (now, now, now),
            )

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":499},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        result = daemon.run_daemon(
            config_path,
            workflow_path="src/robert_agent/references/workflow.yml",
            once=True,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
            now=now,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["decision"], "local_drain")
        self.assertEqual(len(calls), 1)
        self.assertIn("runtime/loop.py", " ".join(calls[0]))
        self.assertIn("--skip-publish", calls[0])
        self.assertNotIn("runtime/cycle.py", " ".join(calls[0]))

    def test_once_runs_live_poll_then_local_drain_when_run_on_start_enabled(self):
        from robert_agent.runtime import daemon
        from robert_agent.adapters.sqlite import database as storage

        config_path = self._write_config({"daemon": {"scan_on_start": True}})
        storage.init_database(self.db_path)
        calls = []

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed", "child": {"returncode": 0}}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":5000},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        result = daemon.run_daemon(
            config_path,
            workflow_path="src/robert_agent/references/workflow.yml",
            once=True,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
            now="2026-07-03T00:00:00+00:00",
        )

        joined = [" ".join(command) for command in calls]
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["decision"], "live_poll")
        self.assertIn("runtime/cycle.py", joined[0])
        self.assertIn("runtime/loop.py", joined[1])

    def test_once_skips_live_poll_when_rate_limit_floor_is_hit(self):
        from robert_agent.runtime import daemon
        from robert_agent.adapters.sqlite import database as storage

        config_path = self._write_config({"daemon": {"scan_on_start": True}})
        storage.init_database(self.db_path)
        calls = []

        def child_runner(command, **_kwargs):
            calls.append(command)
            return {"ok": True, "status": "completed"}

        def rate_limit_runner(command, **_kwargs):
            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":499},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        result = daemon.run_daemon(
            config_path,
            workflow_path="src/robert_agent/references/workflow.yml",
            once=True,
            child_runner=child_runner,
            rate_limit_runner=rate_limit_runner,
            now="2026-07-03T00:00:00+00:00",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["decision"], "live_poll_skipped_rate_limit")
        self.assertEqual(calls, [])

    def test_rate_limit_decision_uses_cache_within_cache_window(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config()
        now = "2026-07-03T00:00:00+00:00"
        calls = []
        self._init_db()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self._insert_repo(conn)

        context, error = daemon._load_context(config_path, now=now)

        self.assertIsNone(error)
        self.assertIsNotNone(context)

        def rate_limit_runner(command, **_kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = '{"resources":{"core":{"remaining":5000},"search":{"remaining":30}}}'
                stderr = ""

            return Completed()

        first = daemon._rate_limit_decision(
            context,
            rate_limit_runner,
            now="2026-07-03T00:00:00+00:00",
        )
        second = daemon._rate_limit_decision(
            context,
            rate_limit_runner,
            now="2026-07-03T00:01:00+00:00",
        )

        self.assertFalse(first["skip"])
        self.assertFalse(second["skip"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(second.get("cached"))

    def test_daemon_disabled_exits_before_acquiring_lease(self):
        from robert_agent.runtime import daemon
        config_path = self._write_config({"daemon": {"enabled": False}})

        result = daemon.run_daemon(
            config_path,
            workflow_path="src/robert_agent/references/workflow.yml",
            once=True,
            now="2026-07-03T00:00:00+00:00",
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "disabled")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            lease_count = conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
            daemon_run_count = conn.execute("SELECT COUNT(*) FROM daemon_runs").fetchone()[0]
        self.assertEqual(lease_count, 0)
        self.assertEqual(daemon_run_count, 0)

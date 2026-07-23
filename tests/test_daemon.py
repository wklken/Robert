from contextlib import closing
from datetime import datetime
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class DaemonStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "dd.sqlite3"

    def _init_db(self):
        from robert_agent import storage

        storage.init_database(self.db_path)
        return self.db_path

    def _insert_repo(self, conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO repos(
              repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root
            )
            VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/repo/.worktrees')
            """
        )
        return "repo-1"

    def test_daemon_run_event_and_summary(self):
        from robert_agent import daemon_state

        self._init_db()
        with closing(daemon_state.connect(self.db_path)) as conn, conn:
            run = daemon_state.start_daemon_run(
                conn,
                config_path="/tmp/config.yml",
                owner_id="daemon-run-1",
                now="2026-07-03T00:00:00+00:00",
            )
            event = daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "daemon_started",
                "ok",
                {"pid": 123},
                now="2026-07-03T00:00:01+00:00",
            )
            summary = daemon_state.latest_daemon_summary(conn)

        self.assertEqual(run["status"], "running")
        self.assertEqual(event["event_type"], "daemon_started")
        self.assertEqual(summary["latest_run"]["daemon_run_id"], run["daemon_run_id"])
        self.assertEqual(summary["latest_event"]["event_type"], "daemon_started")

    def test_daemon_lease_rejects_active_owner_and_replaces_expired(self):
        from robert_agent import daemon_state

        self._init_db()
        with closing(daemon_state.connect(self.db_path)) as conn, conn:
            first = daemon_state.acquire_daemon_lease(
                conn,
                resource_key="repo-1",
                owner_id="daemon-run-1",
                ttl_seconds=60,
                now="2026-07-03T00:00:00+00:00",
            )
            blocked = daemon_state.acquire_daemon_lease(
                conn,
                resource_key="repo-1",
                owner_id="daemon-run-2",
                ttl_seconds=60,
                now="2026-07-03T00:00:10+00:00",
            )
            replaced = daemon_state.acquire_daemon_lease(
                conn,
                resource_key="repo-1",
                owner_id="daemon-run-3",
                ttl_seconds=60,
                now="2026-07-03T00:01:01+00:00",
            )

        self.assertTrue(first["ok"], first)
        self.assertFalse(blocked["ok"], blocked)
        self.assertEqual(blocked["status"], "skipped_active_daemon")
        self.assertTrue(replaced["ok"], replaced)
        self.assertEqual(replaced["status"], "acquired")

    def test_daemon_lease_heartbeat_and_release(self):
        from robert_agent import daemon_state

        self._init_db()
        with closing(daemon_state.connect(self.db_path)) as conn, conn:
            lease = daemon_state.acquire_daemon_lease(
                conn,
                resource_key="repo-1",
                owner_id="daemon-run-1",
                ttl_seconds=60,
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.heartbeat_daemon_lease(
                conn,
                lease["lease_id"],
                ttl_seconds=60,
                now="2026-07-03T00:00:30+00:00",
            )
            heartbeat_at, expires_at = conn.execute(
                "SELECT heartbeat_at, expires_at FROM leases WHERE lease_id = ?",
                (lease["lease_id"],),
            ).fetchone()
            daemon_state.release_daemon_lease(
                conn,
                lease["lease_id"],
                now="2026-07-03T00:00:40+00:00",
            )
            status = conn.execute(
                "SELECT status FROM leases WHERE lease_id = ?",
                (lease["lease_id"],),
            ).fetchone()[0]

        self.assertEqual(heartbeat_at, "2026-07-03T00:00:30+00:00")
        self.assertEqual(expires_at, "2026-07-03T00:01:30+00:00")
        self.assertEqual(status, "released")

    def test_cleanup_old_daemon_events(self):
        from robert_agent import daemon_state

        self._init_db()
        with closing(daemon_state.connect(self.db_path)) as conn, conn:
            run = daemon_state.start_daemon_run(
                conn,
                config_path="/tmp/config.yml",
                owner_id="daemon-run-1",
                now="2026-07-03T00:00:00+00:00",
            )
            daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "daemon_started",
                "ok",
                now="2026-06-20T00:00:00+00:00",
            )
            daemon_state.record_event(
                conn,
                run["daemon_run_id"],
                "daemon_heartbeat",
                "ok",
                now="2026-07-02T00:00:00+00:00",
            )
            deleted = daemon_state.cleanup_old_events(
                conn,
                retention_days=7,
                now="2026-07-03T00:00:00+00:00",
            )
            remaining = conn.execute("SELECT COUNT(*) FROM daemon_events").fetchone()[0]

        self.assertEqual(deleted, 1)
        self.assertEqual(remaining, 1)

    def test_capacity_full_counts_running_and_stale_attempts(self):
        from robert_agent import daemon_state

        self._init_db()
        now = "2026-07-03T00:00:00+00:00"
        with closing(daemon_state.connect(self.db_path)) as conn, conn:
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
                VALUES ('attempt-1', 'task-1', 1, 'stale', ?, ?)
                """,
                (now, now),
            )
            count = daemon_state.running_attempt_count(conn, "repo-1")
            full = daemon_state.capacity_full(conn, "repo-1", max_concurrency=1)

        self.assertEqual(count, 1)
        self.assertTrue(full)


class DaemonChildRunnerTests(unittest.TestCase):
    def test_build_child_commands(self):
        from robert_agent import daemon
        run_once_cmd = daemon.build_run_once_command(
            "/tmp/config.yml",
            "/tmp/workflow.yml",
            dry_run=True,
        )
        loop_cmd = daemon.build_loop_command(
            "/tmp/config.yml",
            "/tmp/workflow.yml",
            dry_run=True,
            max_seconds=180,
        )

        self.assertIn(str(PACKAGE_ROOT / "run_once.py"), run_once_cmd)
        self.assertIn("--dry-run", run_once_cmd)
        self.assertIn(str(PACKAGE_ROOT / "loop_engine.py"), loop_cmd)
        self.assertIn("--skip-external", loop_cmd)
        self.assertIn("--max-seconds", loop_cmd)
        self.assertIn("180", loop_cmd)
        self.assertNotIn("--skip-publish", loop_cmd)

        skip_publish_cmd = daemon.build_loop_command(
            "/tmp/config.yml",
            "/tmp/workflow.yml",
            dry_run=True,
            max_seconds=180,
            skip_publish=True,
        )
        self.assertIn("--skip-publish", skip_publish_cmd)

    def test_run_child_json_parses_single_json_object(self):
        from robert_agent import daemon
        class Completed:
            returncode = 0
            stdout = '{"ok": true, "status": "completed"}\n'
            stderr = ""

        def runner(command, **kwargs):
            return Completed()

        result = daemon.run_child_json(
            ["python3", "-c", "print('ok')"],
            timeout_seconds=5,
            runner=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["child"]["returncode"], 0)

    def test_run_child_json_reports_non_json_and_nonzero(self):
        from robert_agent import daemon
        class Completed:
            returncode = 3
            stdout = "not json"
            stderr = "bad things"

        def runner(command, **kwargs):
            return Completed()

        result = daemon.run_child_json(
            ["python3", "bad.py"],
            timeout_seconds=5,
            runner=runner,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "child_failed")
        self.assertEqual(result["child"]["returncode"], 3)
        self.assertIn("bad things", result["safe_error"])

    def test_run_child_json_reports_timeout(self):
        import subprocess
        from robert_agent import daemon
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = daemon.run_child_json(
            ["python3", "slow.py"],
            timeout_seconds=5,
            runner=runner,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "child_timeout")

    def test_rate_limit_guard_skips_low_search_or_core(self):
        from robert_agent import daemon
        config = {"min_search_remaining": 10, "min_core_remaining": 500}
        parsed = daemon.parse_rate_limit(
            {
                "resources": {
                    "core": {"remaining": 499, "reset": 1780000000},
                    "search": {"remaining": 30, "reset": 1780000100},
                }
            }
        )
        decision = daemon.should_skip_live_poll_for_rate_limit(parsed, config)

        self.assertTrue(decision["skip"])
        self.assertEqual(decision["reason"], "core_rate_limit_floor")

        parsed = daemon.parse_rate_limit(
            {
                "resources": {
                    "core": {"remaining": 5000, "reset": 1780000000},
                    "search": {"remaining": 9, "reset": 1780000100},
                }
            }
        )
        decision = daemon.should_skip_live_poll_for_rate_limit(parsed, config)

        self.assertTrue(decision["skip"])
        self.assertEqual(decision["reason"], "search_rate_limit_floor")

    def test_rate_limit_guard_allows_sufficient_budget(self):
        from robert_agent import daemon
        parsed = daemon.parse_rate_limit(
            {
                "resources": {
                    "core": {"remaining": 500, "reset": 1780000000},
                    "search": {"remaining": 10, "reset": 1780000100},
                }
            }
        )
        decision = daemon.should_skip_live_poll_for_rate_limit(
            parsed,
            {"min_search_remaining": 10, "min_core_remaining": 500},
        )

        self.assertFalse(decision["skip"])
        self.assertEqual(decision["reason"], "rate_limit_ok")

    def test_fetch_rate_limit_parses_gh_payload(self):
        from robert_agent import daemon
        class Completed:
            returncode = 0
            stdout = (
                '{"resources":{"core":{"remaining":1234,"reset":1780000000},'
                '"search":{"remaining":22,"reset":1780000100}}}'
            )
            stderr = ""

        def runner(command, **_kwargs):
            self.assertEqual(command, ["gh", "api", "rate_limit"])
            return Completed()

        result = daemon.fetch_rate_limit(runner=runner)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "rate_limit_loaded")
        self.assertEqual(result["rate_limit"]["core"]["remaining"], 1234)
        self.assertEqual(result["rate_limit"]["search"]["remaining"], 22)

    def test_idle_sleep_seconds_uses_nearest_live_poll_deadline(self):
        from robert_agent import daemon
        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        self.assertEqual(
            daemon._idle_sleep_seconds(
                context,
                now="2026-07-03T00:00:00+00:00",
            ),
            30,
        )

        context.next_live_poll_at = "2026-07-03T00:00:07+00:00"
        self.assertEqual(
            daemon._idle_sleep_seconds(
                context,
                now="2026-07-03T00:00:00+00:00",
            ),
            7,
        )

        self.assertEqual(
            daemon._idle_sleep_seconds(
                context,
                now="2026-07-03T00:00:10+00:00",
            ),
            0,
        )

    def test_sleep_seconds_after_failed_local_drain_uses_local_poll_interval(self):
        from robert_agent import daemon
        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        self.assertEqual(
            daemon._sleep_seconds_after_decision(
                context,
                {"decision": "local_drain", "ok": False},
                now="2026-07-03T00:00:00+00:00",
            ),
            30,
        )

    def test_sleep_seconds_after_no_progress_local_drain_uses_local_poll_interval(self):
        from robert_agent import daemon
        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        self.assertEqual(
            daemon._sleep_seconds_after_decision(
                context,
                {
                    "decision": "local_drain",
                    "ok": True,
                    "result": {"stop_reason": "no_progress"},
                },
                now="2026-07-03T00:00:00+00:00",
            ),
            30,
        )


class DaemonSchedulingTests(DaemonStateTests):
    def _write_config(self, extra_text=""):
        repo_root = self.root / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        config_path = self.root / "config.yml"
        config_path.write_text(
            f"""data_dir: {self.root}
database: dd.sqlite3
max_concurrency: 3
daemon:
  run_on_start: false
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: main
    repo_root: {repo_root}
    worktree_root: {worktree_root}
{extra_text}""",
            encoding="utf-8",
        )
        return config_path

    def test_default_lease_ttl_covers_live_and_local_decision_window(self):
        from robert_agent import daemon
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
        from robert_agent import daemon
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
        from robert_agent import daemon
        config_path = self._write_config("daemon:\n  run_on_start: true\n")
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
        from robert_agent import daemon
        config_path = self._write_config("max_concurrency: 1\n")
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
        from robert_agent import daemon
        config_path = self._write_config(
            "max_concurrency: 3\n"
            "daemon:\n  github_poll_seconds: 300\n"
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
        self.assertIn("loop_engine.py", " ".join(calls[0]))
        self.assertIn("run_once.py", " ".join(calls[1]))
        self.assertIn("loop_engine.py", " ".join(calls[2]))

    def test_run_once_decision_startup_live_poll_runs_at_full_capacity(self):
        from robert_agent import daemon
        config_path = self._write_config(
            "max_concurrency: 1\n"
            "daemon:\n  run_on_start: true\n  github_poll_when_full_seconds: 300\n"
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
        self.assertIn("run_once.py", " ".join(calls[0]))
        self.assertIn("loop_engine.py", " ".join(calls[1]))

    def test_run_once_decision_due_live_poll_is_not_starved_by_running_attempt(self):
        from robert_agent import daemon
        config_path = self._write_config(
            "max_concurrency: 1\n"
            "daemon:\n  github_poll_when_full_seconds: 300\n"
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
        self.assertIn("run_once.py", " ".join(calls[0]))
        self.assertIn("loop_engine.py", " ".join(calls[1]))

    def test_once_runs_local_drain_for_due_wakeup_without_live_poll(self):
        from robert_agent import daemon
        from robert_agent import storage

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
        self.assertIn("loop_engine.py", " ".join(calls[0]))
        self.assertNotIn("run_once.py", " ".join(calls[0]))

    def test_once_runs_local_drain_with_skip_publish_when_rate_limit_floor_is_hit(self):
        from robert_agent import daemon
        from robert_agent import storage

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
        self.assertIn("loop_engine.py", " ".join(calls[0]))
        self.assertIn("--skip-publish", calls[0])
        self.assertNotIn("run_once.py", " ".join(calls[0]))

    def test_once_runs_live_poll_then_local_drain_when_run_on_start_enabled(self):
        from robert_agent import daemon
        from robert_agent import storage

        config_path = self._write_config("daemon:\n  run_on_start: true\n")
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
        self.assertIn("run_once.py", joined[0])
        self.assertIn("loop_engine.py", joined[1])

    def test_once_skips_live_poll_when_rate_limit_floor_is_hit(self):
        from robert_agent import daemon
        from robert_agent import storage

        config_path = self._write_config("daemon:\n  run_on_start: true\n")
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
        from robert_agent import daemon
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
        from robert_agent import daemon
        config_path = self._write_config("daemon:\n  enabled: false\n")

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

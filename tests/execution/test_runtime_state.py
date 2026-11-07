from tests.support import database as fixture_database
from contextlib import closing
from robert_agent.execution import capacity
from tests.support.daemon import DaemonFixture


class DaemonStateTests(DaemonFixture):
    def test_daemon_run_event_and_summary(self):
        from robert_agent.execution import runtime_state as daemon_state

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
        from robert_agent.execution import runtime_state as daemon_state

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
        from robert_agent.execution import runtime_state as daemon_state

        self._init_db()
        with fixture_database.connect(self.db_path) as conn, conn:
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

    def test_capacity_full_counts_running_and_stale_attempts(self):

        self._init_db()
        now = "2026-07-03T00:00:00+00:00"
        with fixture_database.connect(self.db_path) as conn, conn:
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
            count = capacity.running_attempt_count(conn, "repo-1")
            full = capacity.capacity_full(conn, "repo-1", max_concurrency=1)

        self.assertEqual(count, 1)
        self.assertTrue(full)

import time
from robert_agent.adapters.github import client as github_client
from robert_agent.work.application import synchronization as synchronization
from robert_agent.operations import scan_batches as scan_batch_operations
from robert_agent.work import scan_batches as scan_batches
from robert_agent.adapters.processes import attempt as attempt_process
from tests import support  # noqa: F401
from contextlib import closing
import json
import sqlite3
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from robert_agent.runtime import daemon
from robert_agent.runtime import cycle as run_once
from robert_agent.persistence.schema import database as storage
from robert_agent.configuration import command as validate_config
from robert_agent.work import repositories as work_repositories
from robert_agent.adapters.github.read import GitHubReadSession


class GitHubScanRemediationTests(unittest.TestCase):
    def fixture(self):
        from tests.support.repository_cycle import RepositoryCycleFixture

        fixture = RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = validate_config.validate_config(fixture.config_path, skip_external=True)
        storage.init_database(config["db_path"])
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            repo_id = work_repositories.upsert_repo(conn, config["repos"][0])
        return fixture, config, repo_id

    def test_inflight_scan_keeps_local_wait_even_after_poll_deadline(self):
        from tests.support.retention import RetentionFixture, NOW

        fixture = RetentionFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        context = fixture.context
        context.next_live_poll_at = "2026-10-22T23:59:00+00:00"
        context.background_lanes = {"scan": SimpleNamespace(process=object())}
        self.assertEqual(
            daemon._sleep_seconds_after_decision(context, {"decision": "idle"}, now=NOW),
            context.daemon_config["local_poll_seconds"],
        )
        context.background_lanes["scan"].process = None
        self.assertEqual(daemon._sleep_seconds_after_decision(context, {"decision": "idle"}, now=NOW), 0)

    def test_historical_sources_without_workstreams_are_not_reconciled(self):
        _, config, repo_id = self.fixture()
        repo = config["repos"][0]
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            for number in range(1, 41):
                conn.execute(
                    """INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,state)
                    VALUES (?,?,?,'issue',?,'open')""",
                    (f"s{number}", repo_id, f"github:{repo['full_name']}#{number}", number),
                )
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "[]" if args[1] == "search" else '{"state":"closed"}', "")

        for _ in range(2):
            synchronization.scan_repo(
                config, repo, GitHubReadSession(config["db_path"], account=repo["github_account"], runner=runner), []
            )
        self.assertEqual([args for args in calls if args[1] == "api" and "/issues/" in args[2]], [])

    def test_open_derived_pr_keeps_health_checks_and_later_repositories_scanning(self):
        from robert_agent.work.application import completion

        fixture, config, repo_id = self.fixture()
        repo = config["repos"][0]
        repo["pr_automation"] = {"conflict": {"enabled": True}}
        config["repos"] = [dict(repo, full_name="example/first"), repo, dict(repo, full_name="example/last")]
        now = "2026-11-07T00:00:00+00:00"
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at)
                VALUES ('origin',?,?,?)""",
                (repo_id, now, now),
            )
            completion._materialize_open_pr_workstream(
                conn,
                repo_id,
                repo["full_name"],
                "origin",
                {"type": "open_pr", "url": f"https://github.com/{repo['full_name']}/pull/42"},
                now,
            )

        observed = []

        def collect(repo, **kwargs):
            observed.append(repo["full_name"])
            return []

        def runner(args, **kwargs):
            self.assertEqual(args[:2], ["gh", "api"])
            return subprocess.CompletedProcess(args, 0, '{"state":"open"}', "")

        with (
            mock.patch.object(validate_config, "validate_config", return_value=config),
            mock.patch.object(synchronization.github_budget, "budget", return_value=True),
            mock.patch.object(GitHubReadSession, "collect_notifications", return_value={}),
            mock.patch.object(GitHubReadSession, "collect_events", side_effect=collect),
            mock.patch.object(synchronization.pr_health, "fetch_pr_snapshot", return_value=None) as health,
        ):
            result = synchronization.scan(fixture.config_path, runner=runner)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["batch_count"], 3)
        self.assertEqual(observed, [item["full_name"] for item in config["repos"]])
        health.assert_called_once_with(repo["full_name"], 42, runner=mock.ANY)
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM github_scan_batches").fetchone()[0], 3)

    def test_child_attention_is_reported_once_without_detaching_the_child(self):
        from robert_agent.runtime.control import ControlLane

        _, config, _ = self.fixture()
        lane = ControlLane(config["db_path"], "scan", "owner")
        lane.process = SimpleNamespace(pid=1, poll=lambda: 0)
        with mock.patch.object(lane, "_state"), mock.patch.object(attempt_process, "group_stopped", return_value=False):
            self.assertEqual(lane.poll()["status"], "attention")
            self.assertIsNone(lane.poll())
            self.assertIsNotNone(lane.process)

    def test_context_mismatch_is_not_runnable_and_machine_wakeup_does_not_override(self):
        from robert_agent.runtime import loop as loop_engine

        _, config, repo_id = self.fixture()
        scan_batches.save_batch(config["db_path"], repo_id, "old-context", [], {})
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertFalse(
                loop_engine.has_runnable_local_work(
                    conn, repo_ids=[repo_id], config_result=config, consume_observations=True, include_publication=False
                )
            )
        current = synchronization.context_digest(config["repos"][0], [], config=config)
        scan_batches.save_batch(config["db_path"], repo_id, current, [], {})
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertTrue(
                loop_engine.has_runnable_local_work(
                    conn, repo_ids=[repo_id], config_result=config, consume_observations=True, include_publication=False
                )
            )

    def test_unreadable_batch_stops_retrying_and_can_be_explicitly_retried(self):
        _, config, repo_id = self.fixture()
        batch = scan_batches.save_batch(config["db_path"], repo_id, "ctx", [], {}, now=100)
        for attempt in range(5):
            scan_batches.finish_batch(config["db_path"], batch, success=False, now=100 + attempt * 300)
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertFalse(scan_batches.has_pending(conn, [repo_id], now=9999))
            row = conn.execute(
                "SELECT retry_count,attention_reason FROM github_scan_batches WHERE batch_id=?", (batch,)
            ).fetchone()
            self.assertEqual(row[0], 5)
            self.assertTrue(row[1])
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM notifications WHERE notification_type='github_scan_attention'"
                ).fetchone()[0],
                1,
            )
        self.assertTrue(scan_batches.retry_batch(config["db_path"], batch, [repo_id], now=2000)["ok"])
        self.assertIsNotNone(scan_batches.next_batch(config["db_path"], repo_id, now=2000))

    def test_replay_read_failure_marks_run_steps(self):
        fixture, config, repo_id = self.fixture()
        context = synchronization.context_digest(config["repos"][0], [], config=config)
        scan_batches.save_batch(
            config["db_path"],
            repo_id,
            context,
            [{"id": "one", "number": 7, "event_type": "comment", "source_type": "issue"}],
            {},
        )
        result = run_once.run_once(
            fixture.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertFalse(result["ok"])
        with closing(sqlite3.connect(config["db_path"])) as conn:
            states = dict(
                conn.execute("SELECT step_key,status FROM run_repo_steps WHERE run_id=?", (result["run_id"],))
            )
        self.assertEqual(states["discover"], "failed")
        self.assertEqual(states["authorize"], "skipped")
        self.assertEqual(states["route"], "skipped")

    def test_publisher_only_work_does_not_wake_local_only_drain(self):
        from tests.support.publication_budget import PublicationBudgetFixture
        from robert_agent.runtime import loop as loop_engine
        from robert_agent.persistence import execution_control as execution_store

        fixture = PublicationBudgetFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            execution_store.ensure_subject(conn, "task", "t", "human_request", fixture.now.isoformat())
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=["repo"]))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=["repo"], include_publication=False))
            from robert_agent.work import wakeup

            wakeup.request_wakeup(
                conn,
                repo_id="repo",
                reason="publish_retry_ready",
                dedupe_key="publisher-retry",
                now=fixture.now.isoformat(),
            )
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=["repo"], include_publication=False))

    def test_scan_coverage_is_per_source_and_deleted_old_context_reaches_attention(self):
        _, config, repo_id = self.fixture()
        repo = config["repos"][0]
        clock = [1000]
        bad = {"id": "deleted", "number": 7, "source_type": "issue", "event_type": "comment"}
        good = {
            "id": "good",
            "number": 8,
            "source_type": "issue",
            "event_type": "comment",
            "authorization_lookup_complete": True,
        }
        old = scan_batches.save_batch(config["db_path"], repo_id, "old-context", [bad, good], {}, now=100)

        def runner(args, **kwargs):
            if args[2].endswith("/issues/7"):
                return subprocess.CompletedProcess(args, 1, "", "HTTP 410: deleted")
            return subprocess.CompletedProcess(args, 0, '{"state":"open"}', "")

        with mock.patch.object(time, "time", side_effect=lambda: clock[0]):
            for attempt in range(5):
                session = GitHubReadSession(
                    config["db_path"], account=repo["github_account"], runner=runner, now=lambda: clock[0]
                )
                with mock.patch.object(session, "collect_events", return_value=[good]):
                    fresh = synchronization.scan_repo(config, repo, session, [])
                with closing(sqlite3.connect(config["db_path"])) as conn:
                    covered = json.loads(
                        conn.execute(
                            "SELECT complete_sources_json FROM github_scan_batches WHERE batch_id=?", (fresh,)
                        ).fetchone()[0]
                    )
                    self.assertEqual(covered, [["issue", 8]])
                scan_batches.finish_batch(config["db_path"], fresh, success=True, now=clock[0])
                clock[0] += 300
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertTrue(
                conn.execute("SELECT attention_reason FROM github_scan_batches WHERE batch_id=?", (old,)).fetchone()[0]
            )
            self.assertFalse(scan_batches.has_pending(conn, [repo_id], now=clock[0]))

    def test_upgrade_preserves_old_pending_batch_and_adds_retry_state(self):
        _, config, repo_id = self.fixture()
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            conn.execute("DROP TABLE github_scan_batches")
            conn.execute("""CREATE TABLE github_scan_batches (
                batch_id TEXT PRIMARY KEY,repo_id TEXT NOT NULL,context_digest TEXT NOT NULL,
                raw_events_json TEXT NOT NULL,complete_sources_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,retry_at REAL NOT NULL,completed_at REAL)""")
            conn.execute("INSERT INTO github_scan_batches VALUES ('old',?,'ctx','[]','[]',100,100,NULL)", (repo_id,))
        storage.init_database(config["db_path"])
        storage.init_database(config["db_path"])
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT retry_count,attention_reason FROM github_scan_batches WHERE batch_id='old'"
                ).fetchone(),
                (0, None),
            )
        self.assertEqual(scan_batches.next_batch(config["db_path"], repo_id)["batch_id"], "old")

    def test_attention_coalesces_and_retry_cli_is_scoped(self):
        fixture, config, repo_id = self.fixture()
        batch = scan_batches.save_batch(config["db_path"], repo_id, "ctx", [], {})
        for _ in range(5):
            scan_batches.finish_batch(config["db_path"], batch, success=False)
        self.assertEqual(scan_batches.save_batch(config["db_path"], repo_id, "ctx", [], {}), batch)
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM github_scan_batches").fetchone()[0], 1)
            self.assertFalse(scan_batches.has_pending(conn, [repo_id]))
        self.assertFalse(scan_batches.retry_batch(config["db_path"], batch, ["other"])["ok"])
        rows = scan_batch_operations.manage_batches(fixture.config_path)["batches"]
        self.assertEqual(rows[0]["batch_id"], batch)
        self.assertTrue(rows[0]["attention_reason"])
        scan_batch_operations.manage_batches(fixture.config_path, retry=batch, dry_run=True)
        self.assertTrue(scan_batch_operations.manage_batches(fixture.config_path)["batches"][0]["attention_reason"])
        self.assertTrue(scan_batch_operations.manage_batches(fixture.config_path, retry=batch)["ok"])
        self.assertIsNone(scan_batch_operations.manage_batches(fixture.config_path)["batches"][0]["attention_reason"])
        for _ in range(5):
            scan_batches.finish_batch(config["db_path"], batch, success=False)
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM notifications WHERE notification_type='github_scan_attention'"
                ).fetchone()[0],
                "recorded",
            )

    def test_partial_replay_routes_healthy_source_even_when_other_source_is_unreadable(self):
        fixture, config, repo_id = self.fixture()
        context = synchronization.context_digest(config["repos"][0], [], config=config)
        bad = {"id": "bad", "number": 7, "event_type": "comment", "source_type": "issue"}
        good = {
            "id": "good",
            "number": 8,
            "event_type": "comment",
            "source_type": "issue",
            "actor_login": "wklken",
            "body": "@robert-bot please analyze this",
            "intent": "analysis",
            "url": "https://github.com/example/backend/issues/8",
        }
        key = github_client.command_key(["gh", "api", "repos/example/backend/issues/8"])
        scan_batches.save_batch(config["db_path"], repo_id, context, [bad, good], {key: '{"state":"open"}'})
        result = run_once.run_once(
            fixture.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertFalse(result["ok"])
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            states = dict(
                conn.execute("SELECT step_key,status FROM run_repo_steps WHERE run_id=?", (result["run_id"],))
            )
        self.assertEqual(states["discover"], "failed")
        self.assertEqual(states["authorize"], "succeeded")
        self.assertEqual(states["route"], "succeeded")

    def test_discovery_failure_is_attributed_to_its_source_not_entire_repository(self):
        _, config, repo_id = self.fixture()
        repo = config["repos"][0]
        one = {"id": "one", "number": 1, "source_type": "issue", "event_type": "mention"}
        two = {**one, "id": "two", "number": 2}
        scan_batches.save_batch(config["db_path"], repo_id, "old-context", [one, two], {})

        def runner(args, **kwargs):
            if "/permission" in args[2]:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, '{"state":"open"}', "")

        session = GitHubReadSession(config["db_path"], account=repo["github_account"], runner=runner)

        def collect(*args, **kwargs):
            def failed_lookup():
                try:
                    session(["gh", "api", "repos/example/backend/collaborators/user/permission"], check=True)
                except subprocess.CalledProcessError:
                    return [one]

            return session.source_events(repo, one, failed_lookup) + session.source_events(repo, two, lambda: [two])

        with mock.patch.object(session, "collect_events", side_effect=collect):
            fresh = synchronization.scan_repo(config, repo, session, [])
        with closing(sqlite3.connect(config["db_path"])) as conn:
            covered = json.loads(
                conn.execute(
                    "SELECT complete_sources_json FROM github_scan_batches WHERE batch_id=?", (fresh,)
                ).fetchone()[0]
            )
        self.assertEqual(covered, [["issue", 2]])

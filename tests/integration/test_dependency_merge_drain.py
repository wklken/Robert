"""Scan observations are admitted only by the normal local drain."""

from contextlib import closing
import sqlite3
import json

from tests.support.work_items import WorkItemCommandFixture
from robert_agent.work.application import commands
from robert_agent.work import scan_batches

URL = "https://github.com/example/repo/pull/42"


class DependencyMergeDrainTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self.item = self._create(start=True)["item"]
        self.question = self._question(self.item["work_item_id"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.task_id, self.updated_at = conn.execute("SELECT task_id,updated_at FROM tasks").fetchone()
            conn.execute("DELETE FROM wakeups")  # Initial dispatch was already completed before the question.

    def register(self):
        return commands.execute_command(
            self.db_path,
            self.item["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=self.item["version"],
            expected_event_id=self.question["event_id"],
            idempotency_key="dependency",
            body="Continue with the original request after the dependency merges.",
            dependency={"pr_url": URL, "task_id": self.task_id, "task_updated_at": self.updated_at},
        )

    def resume(self):
        from robert_agent.work.application import dependency_merge

        return dependency_merge.resume_pending(self.db_path, "repo-1")

    def local_config(self):
        import yaml
        from tests.support.repository_cycle import RepositoryCycleFixture
        from robert_agent.configuration import command as configuration

        fixture = RepositoryCycleFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        raw = yaml.safe_load(fixture.config_path.read_text())
        raw["data_dir"] = str(self.db_path.parent)
        raw["database"] = self.db_path.name
        raw["repos"][0]["full_name"] = "example/repo"
        raw["repos"][0]["github_account"] = "robot"
        fixture.config_path.write_text(yaml.safe_dump(raw))
        config = configuration.validate_config(fixture.config_path, skip_external=True)
        self.assertTrue(config["ok"], config)
        return fixture.config_path, config

    def scan_dependency(self, config, *, merged):
        import subprocess
        from unittest.mock import Mock
        from robert_agent.work.application import synchronization

        payload = {
            "state": "closed",
            "html_url": URL,
            "pull_request": {"merged_at": "2026-11-21T10:00:00Z" if merged else None},
        }
        session = Mock(side_effect=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""))
        session.failed_sources = set()
        session.collect_events.return_value = []
        return synchronization.scan_repo(config, config["repos"][0], session, [])

    def test_scan_records_merge_then_local_cycle_resumes_once(self):
        from robert_agent.runtime import cycle

        self.register()
        path, config = self.local_config()
        first_batch = self.scan_dependency(config, merged=True)
        self.assertIsNotNone(first_batch)
        self.assertEqual(scan_batches.next_batch(self.db_path, "repo-1")["raw_events"], [])
        self.assertEqual(self.scan_dependency(config, merged=True), first_batch)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM github_sources").fetchone()[0], 0)
        result = cycle.run_once(
            path, dry_run=False, skip_external=True, skip_publish=True, consume_observations=True, max_dispatches=0
        )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM work_item_events WHERE resolves_event_id=?", (self.question["event_id"],)
                ).fetchone()[0],
                1,
            )
        self.assertIsNone(self.scan_dependency(config, merged=True))
        self.assertEqual(self.resume(), [])

    def test_unmerged_dependency_scan_does_not_create_empty_local_work(self):
        from robert_agent.runtime import cycle

        self.register()
        path, config = self.local_config()
        for _ in range(2):
            self.assertIsNone(self.scan_dependency(config, merged=False))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM github_scan_batches").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        result = cycle.run_once(
            path, dry_run=False, skip_external=True, skip_publish=True, consume_observations=True, max_dispatches=0
        )
        self.assertEqual(result["status"], "no_runnable_work", result)

    def test_dry_run_local_cycle_never_answers_dependency_question(self):
        from robert_agent.runtime import cycle

        self.register()
        path, config = self.local_config()
        self.scan_dependency(config, merged=True)
        result = cycle.run_once(
            path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True, max_dispatches=0
        )
        self.assertTrue(result["ok"], result)
        self.assertIsNotNone(scan_batches.next_batch(self.db_path, "repo-1"))
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM work_item_events WHERE resolves_event_id=?", (self.question["event_id"],)
                ).fetchone()[0],
                0,
            )

    def test_dry_run_preserves_normal_observation_completion(self):
        from robert_agent.runtime import cycle
        from robert_agent.work import queries
        from robert_agent.work.application import synchronization

        path, config = self.local_config()
        with closing(sqlite3.connect(self.db_path)) as conn:
            known = queries.load_known_workstreams(conn, "repo-1")
        context = synchronization.context_digest(config["repos"][0], known, config=config)
        scan_batches.save_batch(self.db_path, "repo-1", context, [], {})
        result = cycle.run_once(
            path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True, max_dispatches=0
        )
        self.assertTrue(result["ok"], result)
        self.assertIsNone(scan_batches.next_batch(self.db_path, "repo-1"))

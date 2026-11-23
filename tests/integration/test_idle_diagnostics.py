"""Idle scans and notification delivery must not create repository run history."""

from contextlib import closing
import json
import sqlite3
import subprocess
from unittest import mock

import yaml

from robert_agent.adapters.github.read import GitHubReadSession
from robert_agent.configuration.command import validate_config
from robert_agent.persistence.schema.database import init_database
from robert_agent.runtime import cycle, loop
from robert_agent.work import repositories, scan_batches, wakeup
from robert_agent.work.application import synchronization
from tests.support.repository_cycle import RepositoryCycleFixture


class IdleDiagnosticsTests(RepositoryCycleFixture):
    def test_explicit_fixture_is_processed_even_without_durable_wakeup(self):
        result = cycle.run_once(
            self.config_path,
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
            consume_observations=True,
        )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def prepare(self, repo_count=1):
        raw = yaml.safe_load(self.config_path.read_text())
        for index in range(1, repo_count):
            raw["repos"].append({**raw["repos"][0], "full_name": f"example/idle-{index}"})
        self.config_path.write_text(yaml.safe_dump(raw))
        config = validate_config(self.config_path, skip_external=True)
        init_database(config["db_path"])
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            ids = [repositories.upsert_repo(conn, repo) for repo in config["repos"]]
        return config, ids

    def counts(self, db_path):
        with closing(sqlite3.connect(db_path)) as conn:
            return {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("agent_runs", "run_steps", "run_repo_steps", "leases")
            }

    def test_repeated_empty_scans_do_not_wake_local_execution(self):
        config, ids = self.prepare(10)

        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "[]", "")

        for _ in range(3):
            for repo in config["repos"]:
                session = GitHubReadSession(config["db_path"], account=repo["github_account"], runner=runner)
                synchronization.scan_repo(config, repo, session, [])
            result = loop.run_loop(
                self.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
            )
            self.assertEqual(result["cycles"], 0, result)
        self.assertEqual(self.counts(config["db_path"]), dict.fromkeys(self.counts(config["db_path"]), 0))
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertFalse(scan_batches.has_pending(conn, ids))
            self.assertEqual(conn.execute("SELECT count(*) FROM wakeups").fetchone()[0], 0)

    def test_notification_only_drain_does_not_write_run_history(self):
        config, _ = self.prepare(10)
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            conn.execute(
                "INSERT INTO notifications(notification_id,notification_type,status,created_at,metadata_json) "
                "VALUES ('notice','worker_cleanup_pending','recorded',?,?)",
                (wakeup.utc_now(), json.dumps({"repo_id": "repo:example/backend"})),
            )
        result = cycle.run_once(
            self.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "no_runnable_work")
        self.assertEqual(self.counts(config["db_path"]), dict.fromkeys(self.counts(config["db_path"]), 0))
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertEqual(conn.execute("SELECT status FROM notifications").fetchone()[0], "recorded")

        from robert_agent.delivery.application import lane
        from robert_agent.adapters.notifications import messaging

        # Exercise the actual delivery use case, replacing only the external
        # sender and remote rate-limit query. A notice must still reach transport.
        sender = mock.Mock(return_value=True)
        with (
            mock.patch.dict("os.environ", {"ROBERT_NOTIFICATION_COMMAND": "mock-sender"}),
            mock.patch.object(messaging, "send", sender),
            mock.patch.object(lane.github_budget, "budget", return_value=True),
        ):
            delivered = lane.publish(self.config_path, dry_run=False)
        self.assertTrue(delivered["ok"], delivered)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.counts(config["db_path"]), dict.fromkeys(self.counts(config["db_path"]), 0))

    def test_one_repo_wakeup_does_not_run_nine_idle_repositories(self):
        config, ids = self.prepare(10)
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            wakeup.request_wakeup(conn, repo_id=ids[0], reason="manual_operator_request", dedupe_key="real-work")
        result = cycle.run_once(
            self.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual([repo["repo_id"] for repo in result["repo_summaries"]], [ids[0]])
        self.assertEqual(self.counts(config["db_path"])["run_repo_steps"], 8)

    def test_prepared_repo_waits_when_another_repo_occupies_global_capacity(self):
        raw = yaml.safe_load(self.config_path.read_text())
        raw["scheduler"]["max_concurrency"] = 1
        self.config_path.write_text(yaml.safe_dump(raw))
        config, ids = self.prepare(2)
        with closing(sqlite3.connect(config["db_path"])) as conn, conn:
            for index, repo_id in enumerate(ids):
                conn.execute(
                    "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) "
                    "VALUES (?,?,'active',?,?)",
                    (str(index), repo_id, wakeup.utc_now(), wakeup.utc_now()),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                    "VALUES (?,?,'queued',?,?)",
                    (str(index), str(index), wakeup.utc_now(), wakeup.utc_now()),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES (?,?,1,?,?)",
                    (
                        str(index),
                        str(index),
                        "running" if index == 0 else "prepared",
                        json.dumps({"result_contract": "compact-v1"}),
                    ),
                )
        result = cycle.run_once(
            self.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertEqual(result["status"], "no_runnable_work", result)
        self.assertEqual(self.counts(config["db_path"])["agent_runs"], 0)

    def test_empty_scan_with_tracked_source_still_reconciles_closure(self):
        config, ids = self.prepare()
        self._seed_active_pr_task(9)
        with mock.patch.object(GitHubReadSession, "collect_events", return_value=[]):
            session = GitHubReadSession(
                config["db_path"],
                account=config["repos"][0]["github_account"],
                runner=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, '{"state":"closed"}', ""),
            )
            synchronization.scan_repo(config, config["repos"][0], session, [])
        result = cycle.run_once(
            self.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["repo_summaries"][0]["reconciled_source_count"], 1)
        with closing(sqlite3.connect(config["db_path"])) as conn:
            self.assertFalse(scan_batches.has_pending(conn, ids))
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "canceled")

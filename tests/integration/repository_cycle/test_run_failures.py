from robert_agent.delivery.application import publish
from contextlib import closing
import json
import sqlite3
import subprocess
from tests.support import PACKAGE_ROOT
from robert_agent.execution import workspace as assigned_workspace
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class CycleRunFailureTests(RepositoryCycleFixture):
    def test_run_once_acquires_and_releases_agent_lease(self):
        from robert_agent.runtime import cycle as run_once

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            lease = conn.execute("SELECT resource_type, resource_key, status FROM leases").fetchone()
        self.assertEqual(
            lease,
            ("agent_run", "repo:example/backend", "released"),
        )

    def test_worktree_failure_during_route_fails_run_and_releases_lease(self):
        from robert_agent.runtime import cycle as run_once

        original_prepare_worktree = assigned_workspace.prepare_worktree

        def fake_prepare_worktree(_repo, _event, _route_result, _dry_run):
            raise subprocess.CalledProcessError(128, ["git", "fetch", "upstream", "master"])

        try:
            assigned_workspace.prepare_worktree = fake_prepare_worktree
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            assigned_workspace.prepare_worktree = original_prepare_worktree

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_route")
        self.assertIn("git fetch upstream master", result["safe_error"])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            run_status, error_json = conn.execute("SELECT status, error_json FROM agent_runs").fetchone()
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
            route_step = conn.execute("SELECT status FROM run_steps WHERE step_key = 'route'").fetchone()[0]
        self.assertEqual(run_status, "failed")
        self.assertEqual(json.loads(error_json)["status"], "failed_route")
        self.assertEqual(lease_status, "released")
        self.assertEqual(route_step, "failed")

    def test_route_failure_preserves_related_events_and_prepares_independent_workstream(
        self,
    ):
        from robert_agent.runtime import cycle as run_once

        events = [
            {
                "id": "review-22",
                "number": 22,
                "source_type": "pull_request",
                "event_type": "review_request",
                "actor_login": "wklken",
                "requested_reviewer": "robert-bot",
                "title": "Review first PR",
                "event_at": "2026-10-10T03:00:00Z",
            },
            {
                "id": "followup-22",
                "number": 22,
                "source_type": "pull_request",
                "event_type": "comment",
                "actor_login": "wklken",
                "body": "@robert-bot review this PR",
                "event_at": "2026-10-10T03:01:00Z",
            },
            {
                "id": "review-23",
                "number": 23,
                "source_type": "pull_request",
                "event_type": "review_request",
                "actor_login": "wklken",
                "requested_reviewer": "robert-bot",
                "title": "Review independent PR",
                "event_at": "2026-10-10T03:02:00Z",
            },
        ]
        self.fixture_path.write_text(json.dumps({"events": events}), encoding="utf-8")
        original_prepare = assigned_workspace.prepare_worktree
        prepared_numbers = []

        def prepare(repo, event, route_result, dry_run):
            prepared_numbers.append(event["number"])
            if event["number"] == 22:
                raise subprocess.CalledProcessError(128, ["git", "fetch", "upstream", "pull/22/head"])
            return original_prepare(repo, event, route_result, dry_run)

        try:
            assigned_workspace.prepare_worktree = prepare
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        finally:
            assigned_workspace.prepare_worktree = original_prepare

        self.assertFalse(result["ok"], result)
        self.assertEqual(prepared_numbers, [22, 23])
        self.assertEqual(len(result["prompt_paths"]), 1)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            stored = {row[0] for row in conn.execute("SELECT event_fingerprint FROM github_events")}
            tasks = conn.execute("SELECT workstream_id, route_id FROM tasks").fetchall()
            route_error = json.loads(
                conn.execute("SELECT error_json FROM run_repo_steps WHERE step_key = 'route'").fetchone()[0]
            )
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
        self.assertEqual(
            stored,
            {
                "review_request:review-22",
                "comment:followup-22",
                "review_request:review-23",
            },
        )
        self.assertEqual(tasks, [("github:example/backend!23", "review-pr")])
        self.assertEqual(route_error["failures"][0]["event_fingerprint"], "review_request:review-22")
        self.assertEqual(lease_status, "released")
        retry = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(retry["ok"], retry)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            streams = [row[0] for row in conn.execute("SELECT workstream_id FROM tasks ORDER BY workstream_id")]
        self.assertEqual(streams, ["github:example/backend!22", "github:example/backend!23"])

    def test_blocked_review_worktree_does_not_block_independent_review(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        events = [
            {
                "id": f"review-{number}",
                "number": number,
                "source_type": "pull_request",
                "event_type": "review_request",
                "actor_login": "wklken",
                "requested_reviewer": "robert-bot",
                "event_at": f"2026-10-10T03:0{index}:00Z",
            }
            for index, number in enumerate([22, 23])
        ]
        self.fixture_path.write_text(json.dumps({"events": events}), encoding="utf-8")
        original_prepare = assigned_workspace.prepare_worktree
        prepared_numbers = []

        def prepare(repo, event, route_result, dry_run):
            prepared_numbers.append(event["number"])
            if event["number"] == 22:
                return {"ok": False, "status": "blocked_dirty_worktree"}
            return original_prepare(repo, event, route_result, dry_run)

        with patch.object(assigned_workspace, "prepare_worktree", side_effect=prepare):
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=True,
                skip_external=True,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(prepared_numbers, [22, 23])
        self.assertEqual(result["dispatch_count"], 1)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            tasks = conn.execute(
                "SELECT workstream_id, lifecycle, route_id FROM tasks ORDER BY workstream_id"
            ).fetchall()
            notification = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM notifications WHERE notification_type = 'worktree_unavailable'"
                ).fetchone()[0]
            )
        self.assertEqual(tasks[0], ("github:example/backend!22", "failed", "review-pr"))
        self.assertEqual(tasks[1][0], "github:example/backend!23")
        self.assertNotEqual(tasks[1][1], "failed")
        self.assertEqual(tasks[1][2], "review-pr")
        self.assertEqual(notification["safe_error"], "blocked_dirty_worktree")

    def test_route_failures_are_collected_without_blocking_independent_review(self):
        from robert_agent.runtime import cycle as run_once

        events = [
            {
                "id": f"review-{number}",
                "number": number,
                "source_type": "pull_request",
                "event_type": "review_request",
                "actor_login": "wklken",
                "requested_reviewer": "robert-bot",
                "event_at": f"2026-10-10T03:0{index}:00Z",
                "origin_workstream_id": ("github:example/backend#10" if number in {22, 24} else None),
            }
            for index, number in enumerate([22, 24, 25, 23])
        ]
        self.fixture_path.write_text(json.dumps({"events": events}), encoding="utf-8")
        original_prepare = assigned_workspace.prepare_worktree
        prepared_numbers = []

        def prepare(repo, event, route_result, dry_run):
            prepared_numbers.append(event["number"])
            if event["number"] == 22:
                raise subprocess.CalledProcessError(128, ["git", "fetch", "upstream", "pull/22/head"])
            if event["number"] == 25:
                raise FileNotFoundError("git")
            return original_prepare(repo, event, route_result, dry_run)

        try:
            assigned_workspace.prepare_worktree = prepare
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        finally:
            assigned_workspace.prepare_worktree = original_prepare

        self.assertFalse(result["ok"], result)
        self.assertEqual(prepared_numbers, [22, 25, 23])
        self.assertEqual(len(result["prompt_paths"]), 1)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            error = json.loads(
                conn.execute("SELECT error_json FROM run_repo_steps WHERE step_key = 'route'").fetchone()[0]
            )
        self.assertEqual(
            [failure["event_fingerprint"] for failure in error["failures"]],
            ["review_request:review-22", "review_request:review-25"],
        )

    def test_review_mention_creates_upstream_review_task_without_review_request(self):
        from robert_agent.adapters.github import discovery as discover
        from robert_agent.runtime import cycle as run_once

        body = "@robert-bot review 下这个 PR"
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "mention-review-23",
                            "number": 23,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": body,
                            "title": "feat: monitor subsystem",
                            "intent": discover.infer_intent("feat: monitor subsystem", body, "comment"),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            task = conn.execute(
                "SELECT t.route_id, a.branch_name FROM tasks t JOIN attempts a ON a.task_id=t.task_id"
            ).fetchone()
        self.assertEqual(task, ("review-pr", "review/pr-23-feat-monitor-subsystem"))

    def test_malformed_discovery_event_fails_run_and_releases_lease(self):
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_discovery")
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            run_status, error_json = conn.execute("SELECT status, error_json FROM agent_runs").fetchone()
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
            discover_step = conn.execute("SELECT status FROM run_steps WHERE step_key = 'discover'").fetchone()[0]
        self.assertEqual(run_status, "failed")
        self.assertIn("event id", json.loads(error_json)["safe_error"])
        self.assertEqual(lease_status, "released")
        self.assertEqual(discover_step, "failed")

    def test_publish_exception_fails_run_and_releases_lease(self):
        from robert_agent.runtime import cycle as run_once

        original_publish = publish.publish_ready_actions

        def fake_publish_ready_actions(_db_path, dry_run=False, repo_id=None):
            raise FileNotFoundError("gh")

        try:
            publish.publish_ready_actions = fake_publish_ready_actions
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            publish.publish_ready_actions = original_publish

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "publish_failed")
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            run_status, error_json = conn.execute("SELECT status, error_json FROM agent_runs").fetchone()
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
            publish_step = conn.execute("SELECT status FROM run_steps WHERE step_key = 'publish_actions'").fetchone()[0]
        self.assertEqual(run_status, "failed")
        self.assertEqual(json.loads(error_json)["status"], "publish_failed")
        self.assertEqual(lease_status, "released")
        self.assertEqual(publish_step, "failed")

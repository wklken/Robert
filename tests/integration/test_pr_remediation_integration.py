from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import completion
from robert_agent.work.application import preparation
from robert_agent.work.application import remediation_requests
from robert_agent.work.application import source_events
from tests.support.pr_remediation_integration import PrRemediationIntegrationFixture


class PrRemediationIntegrationTests(PrRemediationIntegrationFixture):
    def test_collector_creates_system_events_and_conflict_first_decision(self):

        runner = self.runner()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                runner,
                "2026-07-26T00:01:00Z",
            )
            event_rows = conn.execute(
                """
                SELECT actor_kind, authorization_status, event_type
                FROM github_events
                ORDER BY event_type
                """
            ).fetchall()

        self.assertEqual(
            [decision["task_kind"] for decision in decisions],
            ["merge_conflict_remediation", "ci_remediation"],
        )
        self.assertTrue(
            all(row[0] == "github_system" for row in event_rows),
        )
        self.assertTrue(
            all(row[1] == "authorized_system_trigger" for row in event_rows),
        )
        self.assertIn(
            "pr_merge_conflict_detected",
            [row[2] for row in event_rows],
        )
        self.assertIn("ci_run_completed", [row[2] for row in event_rows])

    def test_collector_is_idempotent_and_ignores_non_robert_owned_pr(self):

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            first = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )
            second = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:02:00Z",
            )
            counts = (
                conn.execute("SELECT COUNT(*) FROM pr_remediation_episodes").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM github_events").fetchone()[0],
            )

        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(counts, (2, 2))

        other_db = Path(self.tmp.name) / "other.sqlite3"
        self.db_path = other_db
        self.setUp_for_non_owned()
        with closing(sqlite3.connect(other_db)) as conn, conn:
            decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(author="someone-else"),
                "2026-07-26T00:01:00Z",
            )
            episode_count = conn.execute("SELECT COUNT(*) FROM pr_remediation_episodes").fetchone()[0]
        self.assertEqual(decisions, [])
        self.assertEqual(episode_count, 0)

    def test_pending_system_event_waits_for_fresh_health_reconciliation(self):

        event = {
            "source_key": "github:example/backend!13",
            "source_type": "pull_request",
            "number": 13,
            "event_fingerprint": "github-system:workflow_run:101:1",
            "actor_kind": "github_system",
            "task_kind": "ci_remediation",
            "has_open_dd_pr": True,
            "intent": "pr_followup_fix",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            child = completion._create_child_task_for_pending_events(
                conn,
                Path(self.tmp.name),
                self.db_path,
                self.repo,
                "repo:example/backend",
                None,
                "github:example/backend!13",
                [event],
                "2026-07-26T00:02:00Z",
                True,
                [],
                {"remaining": 1},
            )
            workstream_state = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!13'
                """
            ).fetchone()
            task_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE workstream_id = 'github:example/backend!13'
                """
            ).fetchone()[0]

        self.assertIsNone(child)
        self.assertEqual(workstream_state, ("completed", None))
        self.assertEqual(task_count, 0)

    def test_pending_system_event_does_not_discard_later_human_event(self):

        system_event = {
            "repo": "example/backend",
            "source_key": "github:example/backend!13",
            "source_type": "pull_request",
            "number": 13,
            "workstream_id": "github:example/backend!13",
            "event_fingerprint": "github-system:workflow_run:101:1",
            "event_type": "ci_run_completed",
            "actor_kind": "github_system",
            "actor_login": "github",
            "authorization_status": "authorized_system_trigger",
            "task_kind": "ci_remediation",
            "has_open_dd_pr": True,
            "intent": "pr_followup_fix",
        }
        human_event = {
            "repo": "example/backend",
            "source_key": "github:example/backend!13",
            "source_type": "pull_request",
            "number": 13,
            "workstream_id": "github:example/backend!13",
            "event_fingerprint": "comment:human-1",
            "event_type": "comment",
            "actor_kind": "github_user",
            "actor_login": "wklken",
            "body": "@robert-bot please analyze this",
            "authorization_status": "authorized_trigger",
            "intent": "analysis",
            "url": "https://github.com/example/backend/pull/13",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            source_events.insert_source_and_event(
                conn,
                "repo:example/backend",
                system_event,
                "2026-07-26T00:01:00Z",
            )
            source_events.insert_source_and_event(
                conn,
                "repo:example/backend",
                human_event,
                "2026-07-26T00:01:00Z",
            )
            child = completion._create_child_task_for_pending_events(
                conn,
                Path(self.tmp.name),
                self.db_path,
                self.repo,
                "repo:example/backend",
                None,
                "github:example/backend!13",
                [system_event, human_event],
                "2026-07-26T00:02:00Z",
                True,
                [],
                {"remaining": 1},
            )
            trigger = conn.execute(
                """
                SELECT ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                  AND te.relationship = 'trigger'
                """,
                (child["task_id"],),
            ).fetchone()[0]

        self.assertIsNotNone(child)
        self.assertEqual(trigger, "comment:human-1")

    def test_conflict_decision_creates_exact_snapshot_remediation_task(self):
        from robert_agent.workflows import routes as route
        from robert_agent.work import streams as workstream

        runner = self.runner()
        data_dir = Path(self.tmp.name) / "data"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                runner,
                "2026-07-26T00:01:00Z",
            )
            event = decisions[0]
            route_result = route.route_task(event)
            worktree_result = assigned_workspace.prepare_worktree(
                self.repo,
                event,
                route_result,
                True,
            )
            task = preparation.create_task_attempt_and_prompt(
                conn,
                data_dir,
                self.db_path,
                self.repo,
                "repo:example/backend",
                event,
                route_result,
                workstream.plan_event(event, active_workstreams=set()),
                "2026-07-26T00:01:00Z",
                worktree_result=worktree_result,
            )
            task_row = conn.execute(
                """
                SELECT task_kind, metadata_json
                FROM tasks
                WHERE task_id = ?
                """,
                (task["task_id"],),
            ).fetchone()
            episode_status = conn.execute(
                """
                SELECT status
                FROM pr_remediation_episodes
                WHERE episode_id = ?
                """,
                (json.loads(task_row[1])["remediation_episode_id"],),
            ).fetchone()[0]

        prompt = Path(task["prompt_path"]).read_text(encoding="utf-8")
        self.assertEqual(task_row[0], "merge_conflict_remediation")
        self.assertEqual(episode_status, "remediating")
        self.assertEqual(worktree_result["observed_head_sha"], "head-1")
        self.assertEqual(worktree_result["observed_base_sha"], "base-1")
        self.assertIn(
            "git merge --no-edit refs/robert/prs/pr-13-base",
            prompt,
        )

    def test_dirty_remediation_worktree_exhausts_episode_without_task(self):

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )
            event = decisions[0]
            blocked = remediation_requests.handle_blocked_remediation_worktree(
                conn,
                event,
                {
                    "ok": False,
                    "status": "blocked_dirty_worktree",
                    "worktree_path": "/worktrees/pr-13-write",
                },
                "2026-07-26T00:02:00Z",
            )
            episode = conn.execute(
                """
                SELECT status, metadata_json
                FROM pr_remediation_episodes
                WHERE episode_id = ?
                """,
                (event["metadata"]["remediation"]["episode_id"],),
            ).fetchone()
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        self.assertTrue(blocked)
        self.assertEqual(episode[0], "exhausted")
        self.assertEqual(
            json.loads(episode[1])["last_transition_reason"],
            "blocked_dirty_worktree",
        )
        self.assertEqual(task_count, 0)

    def test_failed_system_task_can_retry_same_guarded_event(self):
        from robert_agent.workflows import routes as route
        from robert_agent.work import streams as workstream
        from robert_agent.work.application import (
            lifecycle as work_application_lifecycle,
        )

        data_dir = Path(self.tmp.name) / "retry-data"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            event = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )[0]
            route_result = route.route_task(event)
            task = preparation.create_task_attempt_and_prompt(
                conn,
                data_dir,
                self.db_path,
                self.repo,
                "repo:example/backend",
                event,
                route_result,
                workstream.plan_event(event, active_workstreams=set()),
                "2026-07-26T00:01:00Z",
                worktree_result=assigned_workspace.prepare_worktree(
                    self.repo,
                    event,
                    route_result,
                    True,
                ),
            )
            work_application_lifecycle.finalize_failed_task(
                conn,
                task["task_id"],
                task["workstream_id"],
                "2026-07-26T00:02:00Z",
                {"status": "worker_failed"},
            )
            retry_events = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:03:00Z",
            )

        self.assertEqual(
            retry_events[0]["event_fingerprint"],
            event["event_fingerprint"],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertFalse(
                source_events.should_skip_terminal_trigger(
                    conn,
                    retry_events[0],
                )
            )

    def test_unbound_episode_is_claimed_legacy_before_repair_task_materialization(self):
        from robert_agent.workflows import routes as route
        from robert_agent.adapters.sqlite import execution_control as execution_store
        from robert_agent.work import streams as workstream

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            event = remediation_requests.collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )[0]
            task = preparation.create_task_attempt_and_prompt(
                conn,
                Path(self.tmp.name),
                self.db_path,
                self.repo,
                "repo:example/backend",
                event,
                route.route_task(event),
                workstream.plan_event(event, active_workstreams=set()),
                "2026-07-26T00:02:00Z",
                worktree_result=None,
            )
            episode_id = event["metadata"]["remediation"]["episode_id"]
            binding = execution_store.get_binding(
                conn,
                "remediation_episode",
                episode_id,
            )

        self.assertIsNotNone(task)
        self.assertEqual(binding["binding_status"], "active")


if __name__ == "__main__":
    unittest.main()

"""Explicit recovery replaces ended repair authority with a fresh operator task."""

import json
from pathlib import Path
from unittest.mock import patch

from tests.support.pr_remediation_integration import PrRemediationIntegrationFixture
from tests.support import database as fixture_database
from robert_agent.persistence import execution_control
from robert_agent.work.application import commands, ready_work, source_events
from robert_agent.work.inputs import CommandContext
from robert_agent.work.errors import WorkItemConflictError, WorkItemNotFoundError
from robert_agent.work.application.admission import ordinary_dispatch_allowed
from robert_agent.workflows import pr_health
from robert_agent.execution import ownership, workspace
from robert_agent.foundation import values


class TerminalRemediationRetriggerTests(PrRemediationIntegrationFixture):
    def setUp(self):
        super().setUp()
        self.now = values.utc_now()
        self.stream = "github:example/backend!13"
        self.context = CommandContext("operator", "wklken", frozenset({"repo:example/backend"}), frozenset())
        with fixture_database.connect(self.db_path) as conn:
            conn.execute("INSERT INTO agent_runs(run_id,status,started_at) VALUES('run','running',?)", (self.now,))
            conn.execute("UPDATE github_sources SET source_key='fixture-pr' WHERE source_id='source:pr'")
            conn.execute("UPDATE workstreams SET lifecycle='failed' WHERE workstream_id=?", (self.stream,))
            source_events.insert_source_and_event(
                conn,
                "repo:example/backend",
                {
                    "repo": "example/backend",
                    "source_type": "pull_request",
                    "source_key": self.stream,
                    "workstream_id": self.stream,
                    "number": 13,
                    "event_type": "pr_merge_conflict_detected",
                    "event_fingerprint": "old-repair",
                    "state": "open",
                    "task_kind": "merge_conflict_remediation",
                    "existing_pr_head_branch": "codex/fix",
                    "head_sha": "old-head",
                    "base_sha": "old-base",
                    "metadata": {"remediation": {"episode_id": "old-episode"}},
                },
                self.now,
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,metadata_json,created_at,updated_at) VALUES('old',?,'merge_conflict_remediation','failed',?,?,?)",
                (self.stream, json.dumps({"remediation_episode_id": "old-episode"}), self.now, self.now),
            )
            conn.execute("UPDATE tasks SET route_id='update-existing-pr' WHERE task_id='old'")
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,finished_at,metadata_json) VALUES('old-attempt','old',1,'failed',?,'{}')",
                (self.now,),
            )
            conn.execute(
                "INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES('old','event:old-repair','trigger',?)",
                (self.now,),
            )
            conn.execute(
                "INSERT INTO route_decisions(route_decision_id,task_id,route_id,confidence,allowed_github_actions_json,expected_output,created_at) VALUES('route','old','update-existing-pr','high','[]','update_existing_pr',?)",
                (self.now,),
            )
            conn.execute(
                "INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,observed_head_sha,observed_base_sha,status,first_seen_at,updated_at) VALUES('old-episode',?,'merge_conflict','old-head:old-base','old-head','old-base','open',?,?)",
                (self.stream, self.now, self.now),
            )
            execution_control.ensure_subject(conn, "remediation_episode", "old-episode", "pr_remediation", self.now)
            conn.execute("UPDATE pr_remediation_episodes SET status='superseded' WHERE episode_id='old-episode'")
            execution_control.close_binding(conn, "remediation_episode", "old-episode", self.now)
            self.original = execution_control.get_binding(conn, "remediation_episode", "old-episode")
            commands.ensure_github_work_item(
                conn,
                repo_id="repo:example/backend",
                source_id="source:pr",
                workstream_id=self.stream,
                actor_identity="wklken",
                route_confidence="high",
                now=self.now,
            )
        self.snapshot = {
            "state": "open",
            "merged": False,
            "mergeable": False,
            "head_sha": "new-head",
            "base_sha": "new-base",
            "head_ref": "codex/fix",
            "base_ref": "main",
            "head_repo_full_name": "robert-bot/backend",
            "author_login": "robert-bot",
            "html_url": "https://github.com/example/backend/pull/13",
        }

    def request(self, key="recover"):
        with patch.object(pr_health, "fetch_pr_snapshot", return_value=self.snapshot):
            return commands.retrigger_failed_github_task(
                self.db_path, "old", context=self.context, idempotency_key=key, notifier=lambda _: None
            )

    def test_retrigger_pins_current_snapshot_and_preserves_closed_episode(self):
        result = self.request()
        self.assertEqual(self.request(), result)
        queue = []
        with fixture_database.connect(self.db_path) as conn:
            with patch.object(
                workspace,
                "prepare_worktree",
                return_value={"ok": True, "worktree_path": "/worktrees/fix", "branch_name": "codex/fix"},
            ) as prepare:
                ready_work.prepare_github_retrigger_tasks(
                    conn,
                    Path(self.tmp.name),
                    self.db_path,
                    self.repo,
                    "repo:example/backend",
                    "run",
                    values.utc_now(),
                    True,
                    queue,
                    {"remaining": 1},
                    [],
                    config_result=None,
                )
            self.assertEqual(len(queue), 1)
            event = prepare.call_args.args[1]
            self.assertEqual((event["head_sha"], event["base_sha"]), ("new-head", "new-base"))
            task = queue[0]["task_info"]["task_id"]
            row = conn.execute("SELECT task_kind,metadata_json FROM tasks WHERE task_id=?", (task,)).fetchone()
            self.assertEqual(row[0], "human_request")
            self.assertNotIn("remediation_episode_id", json.loads(row[1]))
            self.assertTrue(json.loads(row[1]).get("trigger_work_item_event_id"))
            self.assertTrue(ownership.claim_task(conn, task, self.now))
            self.assertTrue(ordinary_dispatch_allowed(conn, task, self.repo))
            self.assertEqual(execution_control.get_binding(conn, "remediation_episode", "old-episode"), self.original)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM work_item_events WHERE event_type='retry_requested'").fetchone()[0],
                1,
            )
            self.assertEqual(
                json.loads(
                    conn.execute(
                        "SELECT payload_json FROM github_events WHERE event_fingerprint='old-repair'"
                    ).fetchone()[0]
                )["metadata"]["remediation"]["episode_id"],
                "old-episode",
            )

    def test_closed_or_unavailable_remote_refuses_retrigger(self):
        for snapshot in (
            None,
            {**self.snapshot, "state": "closed"},
            {**self.snapshot, "head_sha": ""},
            {**self.snapshot, "head_repo_full_name": "other/repo"},
            {**self.snapshot, "head_repo_full_name": "other/backend"},
            {**self.snapshot, "head_repo_full_name": "robert-bot/other"},
            {**self.snapshot, "head_repo_full_name": ""},
            {**self.snapshot, "author_login": "other"},
        ):
            self.snapshot = snapshot
            with self.assertRaises(WorkItemConflictError):
                self.request()

    def test_same_repository_head_remains_supported(self):
        self.snapshot["head_repo_full_name"] = "example/backend"
        self.assertEqual(self.request()["status"], "queued")

    def test_denied_scope_does_not_fetch_or_queue_recovery(self):
        self.context = CommandContext("operator", "other", frozenset(), frozenset())
        with patch.object(pr_health, "fetch_pr_snapshot", side_effect=AssertionError("denied scope")):
            with self.assertRaises(WorkItemNotFoundError):
                commands.retrigger_failed_github_task(
                    self.db_path, "old", context=self.context, idempotency_key="denied"
                )
        with fixture_database.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM wakeups").fetchone()[0], 0)

    def test_recovery_requires_the_exact_audited_operator_event(self):
        result = self.request()
        with fixture_database.connect(self.db_path) as conn:
            self.assertTrue(ownership.parent_allows_child(conn, "old", retrigger_wakeup_id=result["wakeup_id"]))
            conn.execute("UPDATE work_item_events SET metadata_json='{}' WHERE event_type='retry_requested'")
            self.assertFalse(ownership.parent_allows_child(conn, "old", retrigger_wakeup_id=result["wakeup_id"]))

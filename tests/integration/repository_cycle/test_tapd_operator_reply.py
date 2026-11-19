"""Local operator replies resume TAPD requests through the saved scan context."""

from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from unittest import mock

from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.configuration import validation
from robert_agent.delivery.application import submission
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.readmodels import board
from robert_agent.runtime import cycle as run_once
from robert_agent.runtime import loop
from robert_agent.work.application import commands, tapd_scan
from tests.support.tapd import STORY, comment
from tests.support.tapd_cycle import TapdCycleCase
from tests.support.repository_cycle import _verification_evidence


class TapdOperatorReplyTests(TapdCycleCase):
    def setUp(self):
        super().setUp()
        self.db = self.data_dir / "dd.sqlite3"
        self.scan(comment("1", "@robert-wklken please fix this bug"))
        self.cycle()
        ((self.parent, self.parent_attempt, self.workspace, self.branch),) = self.query(
            "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
        )
        recorded = submission.record_result(
            self.db,
            {
                "task_id": self.parent,
                "attempt_id": self.parent_attempt,
                "output_type": "waiting_for_user",
                "submission": {
                    "planned_github_actions": [
                        {"type": "comment", "body": "Local fix preserved; which target branch?"}
                    ],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
                    "operator_question": {"kind": "scope_decision", "summary": "Which target branch?"},
                    "verification": [_verification_evidence()],
                    "handoff": "Local fix preserved; waiting for the target branch.",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(recorded["ok"], recorded)
        with mock.patch.object(tapd_client, "client_for", return_value=self.client):
            published = run_once.run_once(self.config_path, dry_run=False, skip_external=True, max_dispatches=0)
        self.assertTrue(published["ok"], published)
        ((self.item_id, self.repo_id),) = self.query("SELECT work_item_id, repo_id FROM work_items")
        self.context = commands.CommandContext(
            actor_kind="operator",
            actor_identity="local-operator",
            allowed_repo_ids=frozenset({self.repo_id}),
            allowed_workers=frozenset({"default"}),
        )
        item = board.get_work_item_detail(self.db, self.item_id)
        self.reply = commands.execute_command(
            self.db,
            self.item_id,
            context=self.context,
            command="reply",
            expected_version=item["version"],
            idempotency_key="confirm-master",
            body="Use master first; I will trigger the release PR separately.",
        )
        self.task = self.reply["task_id"]
        ((requested,),) = self.query("SELECT created_at FROM wakeups WHERE task_id=?", (self.task,))
        self.requested = datetime.fromisoformat(requested)
        self.set_snapshot_time(self.requested - timedelta(seconds=1))

    def set_snapshot_time(self, observed):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            (metadata,) = conn.execute("SELECT metadata_json FROM github_sources").fetchone()
            metadata = json.loads(metadata)
            metadata["tapd"]["observed_at"] = observed.isoformat()
            conn.execute("UPDATE github_sources SET metadata_json=?", (json.dumps(metadata),))

    def refresh(self):
        config = validation.validate_config(self.config_path, skip_external=True)
        return tapd_scan.scan_project(
            self.db,
            config["tapd"],
            config["tapd"]["projects"][0],
            {repo["full_name"]: repo for repo in config["repos"]},
            self.client,
            now=(self.requested + timedelta(seconds=1)).isoformat(),
        )

    def attempts(self):
        return self.query("SELECT worktree_path, branch_name FROM attempts WHERE task_id=?", (self.task,))

    def test_reply_resumes_once_with_tapd_context_and_the_preserved_workspace(self):
        self.refresh()
        self.cycle()
        self.assertEqual(self.attempts(), [(self.workspace, self.branch)])
        self.assertEqual(self.query("SELECT route_id FROM tasks WHERE task_id=?", (self.task,)), [("new-pr",)])
        self.assertEqual(self.query("SELECT status FROM wakeups WHERE task_id=?", (self.task,)), [("consumed",)])
        ((prompt,),) = self.query("SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'", (self.task,))
        text = Path(prompt).read_text()
        self.assertIn("origin_type: tapd", text)
        self.assertIn("Use master first; I will trigger the release PR separately.", text)
        self.assertIn("Local fix preserved; which target branch?", text)
        self.assertIn("@robert-wklken please fix this bug", text)
        self.assertIn("Keep TAPD out of GitHub", text)
        self.assertEqual(
            self.query(
                "SELECT e.event_fingerprint FROM task_events te JOIN github_events e USING(event_id) "
                "WHERE te.task_id=?",
                (self.task,),
            ),
            [("tapd-comment:tapd.example/100/1",)],
        )
        self.cycle()
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(2,)])

    def test_stale_snapshot_waits_without_repeated_empty_drains(self):
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False))
        self.cycle()
        self.assertEqual(self.attempts(), [])
        detail = board.get_work_item_detail(self.db, self.item_id)
        self.assertEqual(detail["reason_code"], "tapd_context_refresh_pending")
        self.assertIn("TAPD", detail["reason_summary"])
        self.refresh()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False))

    def test_failed_scan_does_not_release_a_waiting_reply(self):
        self.client.fail_items.add(STORY)
        observed = self.refresh()
        self.assertTrue(observed["failures"], observed)
        self.cycle()
        self.assertEqual(self.attempts(), [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False))

    def test_missing_or_invalid_snapshot_waits_without_launching(self):
        for metadata in ("{}", "[]", '{"tapd":[]}', '{"tapd":{"observed_at":"invalid"}}'):
            with self.subTest(metadata=metadata):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE github_sources SET metadata_json=?", (metadata,))
                with closing(sqlite3.connect(self.db)) as conn:
                    self.assertFalse(
                        loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False)
                    )
        self.assertEqual(self.attempts(), [])

    def test_replaying_the_reply_command_keeps_the_same_task(self):
        replay = commands.execute_command(
            self.db,
            self.item_id,
            context=self.context,
            command="reply",
            expected_version=self.reply["item"]["version"] - 1,
            idempotency_key="confirm-master",
            body="Use master first; I will trigger the release PR separately.",
        )
        self.assertEqual(replay["task_id"], self.task)
        self.refresh()
        self.cycle()
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(2,)])

    def test_ended_ticket_never_launches_the_reply(self):
        self.client.status[STORY] = "status_3"
        self.refresh()
        self.cycle()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (self.task,)), [("failed",)])
        self.assertEqual(
            self.query(
                "SELECT json_extract(metadata_json, '$.reason_code') FROM work_item_events "
                "WHERE event_type='execution_failed'"
            ),
            [("tapd_operator_source_closed",)],
        )

    def test_changed_trusted_user_stops_the_reply(self):
        self.refresh()
        self.configure("bob")
        self.cycle()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (self.task,)), [("failed",)])
        self.assertEqual(
            self.query(
                "SELECT json_extract(metadata_json, '$.reason_code') FROM work_item_events "
                "WHERE event_type='execution_failed'"
            ),
            [("tapd_operator_authorization_unavailable",)],
        )

    def test_cancellation_during_workspace_io_never_launches_the_reply(self):
        self.refresh()

        def cancel(*_args, **_kwargs):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (self.task,))
                conn.execute("UPDATE work_items SET canceled_at=?", (self.requested.isoformat(),))
                conn.execute("UPDATE wakeups SET status='canceled' WHERE task_id=?", (self.task,))
            return None

        with mock.patch.object(assigned_workspace, "prepare_delivery_continuation", side_effect=cancel):
            self.cycle()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (self.task,)), [("canceled",)])

    def test_snapshot_changed_during_workspace_io_is_rechecked(self):
        self.refresh()

        def change(*_args, **_kwargs):
            self.set_snapshot_time(self.requested + timedelta(seconds=2))
            return None

        with mock.patch.object(assigned_workspace, "prepare_delivery_continuation", side_effect=change):
            self.cycle()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.query("SELECT status FROM wakeups WHERE task_id=?", (self.task,)), [("pending",)])

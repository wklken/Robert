"""Global service health alerts never export a workstream's repository context."""

from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

from tests.support.work_items import WorkItemCommandFixture
from robert_agent.adapters.notifications import messaging as transport
from robert_agent.delivery import messages
from robert_agent.delivery.application import notifications
from robert_agent.persistence import database
from robert_agent.supervision import decisions, store
from robert_agent.supervision.application.decision_loop import DecisionLoop


class DecisionNotificationScopeTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self._failed_github_task()
        self.now = "2026-11-13T03:00:00+00:00"
        self.root = "github:example/repo!21"
        self.diagnostic = "Private customer roadmap validation failed"
        self.config = {
            "db_path": str(self.db_path),
            "supervision": {"mode": "decide"},
            "repos": [{"full_name": "example/repo"}, {"full_name": "example/public"}],
            "messaging": {
                "enabled": True,
                "provider": "openclaw",
                "channel": "wecom",
                "account": "default",
                "target": "wecom:public-room",
                "repos": ["example/public"],
                "send_after": "2026-11-13T00:00:00Z",
            },
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES ('public','example/public','bot','main','/public','/public-trees')"
            )
        with closing(database.connect(self.db_path)) as conn, conn:
            request = decisions.packet(decisions.snapshot(conn, self.root, self.config, self.now), {})
            store.update_checkpoint(
                conn,
                *decisions.RUNNER_KEY,
                now=self.now,
                metadata={"failures": 1, "claim": {"bundle_id": request["bundle_id"], "cooldown_seconds": 120}},
            )
        self.notice_id = "decision-unavailable-" + request["bundle_id"]
        loop = DecisionLoop()
        loop.result = (
            request,
            {"status": "exited", "stopped": True, "exit_code": 7, "safe_error": self.diagnostic},
            None,
        )
        self.assertEqual(loop._finish(self.config, self.now)["status"], "exited")

    def assert_global_service_payload(self, payload):
        self.assertIsNotNone(payload)
        self.assertEqual(payload["actions"], ["ack"])
        self.assertEqual(payload["reason_code"], "exited")
        self.assertEqual(payload["exit_code"], 7)
        self.assertIn("全局", payload["summary"])
        for key in ("workstream_id", "repository", "title", "pr_url", "safe_error", "workstream_status"):
            self.assertNotIn(key, payload)
        for value in (self.root, self.diagnostic, "example/repo", "Update AIDev integration"):
            self.assertNotIn(value, json.dumps(payload))

    def assert_scoped_delivery(self):
        with closing(database.connect(self.db_path)) as conn:
            self.assertEqual(messages.allowed_repo_ids(conn, self.config), {"public"})
            for selected in ([], ["example/public"], ["example/repo"]):
                config = {**self.config, "messaging": {**self.config["messaging"], "repos": selected}}
                with self.subTest(selected=selected):
                    self.assert_global_service_payload(messages.envelope(conn, self.notice_id, config=config))
            # Custom-command delivery and local projections use the same safe envelope.
            self.assert_global_service_payload(messages.envelope(conn, self.notice_id))
            self.assertEqual(decisions.read(conn, self.root)["last_failure"]["safe_error"], self.diagnostic)
        with patch.dict("os.environ", {}, clear=True), patch.object(transport, "send", return_value=True) as sender:
            result = notifications.deliver_pending(self.db_path, config=self.config, now=self.now)
        self.assertEqual(result["sent"], 1)
        sender.assert_called_once()
        self.assert_global_service_payload(sender.call_args.args[0])

    def test_new_global_alert_keeps_diagnostics_local(self):
        self.assert_scoped_delivery()
        with closing(sqlite3.connect(self.db_path)) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM notifications").fetchone()[0])
        self.assertNotIn("workstream_id", metadata)
        self.assertNotIn("safe_error", metadata)

    def test_pending_legacy_alert_cannot_replay_repository_details(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM notifications").fetchone()[0])
            metadata.update(workstream_id=self.root, safe_error=self.diagnostic, summary=self.diagnostic)
            conn.execute("UPDATE notifications SET metadata_json=?", (json.dumps(metadata),))
        self.assert_scoped_delivery()

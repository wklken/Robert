from contextlib import closing
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace

from robert_agent.adapters.notifications import messaging
from robert_agent.delivery.application import receipt_notifications
from robert_agent.work import receipt_events
from tests.support.board import BoardReadModelFixture


class ReceiptNotificationsTests(BoardReadModelFixture):
    def setUp(self):
        super().setUp()
        self.conn = self.enterContext(closing(sqlite3.connect(self.db_path)))
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) "
            "VALUES ('stream','repo-1','active','2026-11-22T00:00:00Z','2026-11-22T00:00:00Z')"
        )
        self.config = {
            "db_path": self.db_path,
            "data_dir": self.db_path.parent,
            "repos": [{"full_name": "example/repo"}],
            "messaging": {
                "enabled": True,
                "provider": "openclaw",
                "channel": "telegram",
                "account": "personal",
                "target": "123",
                "conversation": "telegram:123",
                "thread": "",
                "repos": ["example/repo"],
                "allowed_senders": ["123"],
                "send_after": "2026-11-22T00:00:00Z",
                "receipts": {
                    "enabled": True,
                    "send_after": "2026-11-22T00:00:00Z",
                    "events": ["pr_opened", "report_delivered"],
                    "chromium": "chromium",
                },
            },
        }
        self.rendered = []
        self.sends = []

    def _result(self, ID, action, output, at, url, *, status="published"):
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES "
            "(?,'stream','completed',?,?)",
            (ID, at, at),
        )
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES (?,?,1,'completed')", (ID, ID)
        )
        self.conn.execute(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,handoff,created_at,metadata_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (ID, ID, ID, output, "Reported outcome", at, '{"audit":{"status":"accepted"}}'),
        )
        self.conn.execute(
            "INSERT INTO github_actions(action_id,task_id,result_id,action_type,target_url,audit_status,"
            "publish_status,created_at,metadata_json) VALUES (?,?,?,?,?,'accepted',?,?,?)",
            (ID, ID, ID, action, url, status, at, json.dumps({"publish": {"published_at": at}})),
        )
        receipt_events.record_completed(
            self.conn,
            self.config,
            "repo-1",
            "example/repo",
            "stream",
            "work-item",
            {"task_id": ID, "result_id": ID, "output_type": output},
        )
        self.conn.commit()

    def _pr(self, ID, minute, number=42):
        self._result(
            ID, "open_pr", "new_pr", f"2026-11-22T00:{minute}:00Z", f"https://github.com/example/repo/pull/{number}"
        )

    def _render(self, receipts, directory, executable):
        self.assertEqual(executable, "chromium")
        self.rendered.append(receipts)
        outputs = []
        for index, receipt in enumerate(receipts):
            path = Path(directory) / f"test-{index}.png"
            path.write_bytes(receipt["event"].encode())
            outputs.append(path)
        return outputs

    def _send(self, payload, *, config, media_paths, receipt):
        def runner(argv, **kwargs):
            self.sends.append(argv)
            images = [Path(path).read_bytes() for flag, path in zip(argv, argv[1:]) if flag == "--media"]
            self.assertEqual(images, [row["event"].encode() for row in self.rendered[-1]])
            return SimpleNamespace(returncode=0, stdout='{"ok":true,"payload":{"messageId":"test-id"}}')

        return messaging.send(payload, config=config, media_paths=media_paths, receipt=receipt, runner=runner)

    def _deliver(self, now, **kwargs):
        return receipt_notifications.deliver_pending(
            self.config,
            now=f"2026-11-22T{now}:00Z",
            render=kwargs.get("render", self._render),
            send=kwargs.get("send", self._send),
        )

    def test_first_pr_and_report_are_sent_together_as_separate_images(self):
        self._pr("old", "00", 40)
        self._pr("first", "01")
        self._pr("duplicate", "02")
        self._result(
            "update",
            "push_existing_pr",
            "update_existing_pr",
            "2026-11-22T00:03:00Z",
            "https://github.com/example/repo/pull/42",
        )
        self._result(
            "pending",
            "open_pr",
            "new_pr",
            "2026-11-22T00:04:00Z",
            "https://github.com/example/repo/pull/45",
            status="not_published",
        )
        self._result(
            "report",
            "comment",
            "multi_agent_vote_report",
            "2026-11-22T00:12:00Z",
            "https://github.com/example/repo/issues/3#issuecomment-41",
        )
        self.assertEqual(self._deliver("00:30")["status"], "waiting")
        self.assertTrue(receipt_notifications.has_due(self.conn, self.config, "2026-11-22T00:31:00Z"))
        self.assertEqual(self._deliver("00:31"), {"status": "sent", "count": 2})
        self.assertEqual([item["event"] for item in self.rendered[0]], ["pr_opened", "report_delivered"])
        self.assertEqual([item["summary"] for item in self.rendered[0]], ["Reported outcome", "Reported outcome"])
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(self.sends[0].count("--media"), 2)
        self.assertEqual(self._deliver("01:20")["status"], "waiting")
        self.assertEqual(len(self.sends), 1)

    def test_added_merge_event_and_previously_pending_events_obey_current_config_and_rate_limit(self):
        self._pr("first", "01")
        self.assertEqual(self._deliver("00:31")["status"], "sent")
        self._pr("second", "02", number=43)
        self.config["messaging"]["receipts"]["events"] = ["pr_merged"]
        remote = {
            "html_url": "https://github.com/example/repo/pull/42",
            "merged_at": "2026-11-22T00:15:00Z",
            "title": "Merged delivery",
        }
        receipt_events.record_merge(self.conn, self.config, "repo-1", "example/repo", remote)
        receipt_events.record_merge(self.conn, self.config, "repo-1", "example/repo", remote)
        self.conn.commit()
        self.assertEqual(self._deliver("00:55")["status"], "waiting")
        self.assertEqual(self._deliver("01:01"), {"status": "sent", "count": 1})
        self.assertEqual([row["event"] for row in self.rendered[1]], ["pr_merged"])

    def test_render_failure_can_retry_but_uncertain_send_is_not_repeated(self):
        self._pr("first", "01")

        def broken_image(*args):
            raise ValueError("render failed")

        self.assertEqual(self._deliver("00:31", render=broken_image)["status"], "render_failed")
        self.assertEqual(self._deliver("00:32")["status"], "waiting")
        self.assertEqual(self._deliver("01:01", send=lambda *args, **kwargs: False)["status"], "delivery_unknown")
        self.assertEqual(self._deliver("02:00")["status"], "waiting")
        self.assertEqual(len(self.rendered), 1)

    def test_pending_image_does_not_follow_a_target_change(self):
        self._pr("first", "01")
        self.config["messaging"]["target"] = "different"
        self.assertEqual(self._deliver("00:55")["status"], "waiting")
        self.assertEqual(self.sends, [])

    def test_incomplete_render_keeps_both_receipts_pending(self):
        self._pr("first", "01")
        self._pr("second", "02", number=43)

        def partial_render(receipts, directory, executable):
            return self._render(receipts, directory, executable)[:1]

        self.assertEqual(self._deliver("00:31", render=partial_render)["status"], "render_failed")
        self.assertEqual(self.sends, [])
        self.assertEqual(self._deliver("01:01"), {"status": "sent", "count": 2})

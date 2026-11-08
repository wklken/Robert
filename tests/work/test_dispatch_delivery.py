from contextlib import closing
import json
import sqlite3

from robert_agent.persistence import database
from robert_agent.work.application import dispatch_delivery
from tests.support.dispatch_requests import DispatchFixture


NOW = "2026-11-08T06:00:00Z"
ISSUE = "https://github.com/example/repo/issues/7"
COMMENT = f"{ISSUE}#issuecomment-42"
PR = "https://github.com/example/repo/pull/8"


class DispatchDeliveryTests(DispatchFixture):
    def setUp(self):
        super().setUp()
        self.request_id = self.draft()["request_id"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, created_at, updated_at) VALUES ('ws', ?, ?, ?)",
                (self.repo_id, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) "
                "VALUES ('task', 'ws', 'completed', ?, ?)",
                (NOW, NOW),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES ('attempt', 'task', 1, 'completed')"
            )
            conn.execute(
                "UPDATE dispatch_requests SET status = 'admitted', workstream_id = 'ws', task_id = 'task', issue_json = ? "
                "WHERE request_id = ?",
                (json.dumps({"number": 7, "html_url": ISSUE}), self.request_id),
            )

    def _result(self, output):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, handoff, created_at) "
                "VALUES ('result', 'task', 'attempt', ?, 'Startup trace analyzed.', ?)",
                (output, NOW),
            )

    def _action(self, action_id, kind, url, *, published=True, receipt=True, receipt_action_id=None):
        metadata = {}
        if receipt and published:
            metadata["publish"] = {
                "status": "published",
                "receipt": {
                    "schema_version": 1,
                    "action_id": receipt_action_id or action_id,
                    "result_id": "result",
                    "task_id": "task",
                    "action_type": kind,
                    "target_url": url,
                    "external_id": "42",
                    "confirmation_method": "publisher_acknowledgement",
                    "confirmed_at": NOW,
                },
            }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, "
                "publish_status, created_at, metadata_json) VALUES (?, 'result', 'task', ?, ?, 'accepted', ?, ?, ?) "
                "ON CONFLICT(action_id) DO UPDATE SET publish_status=excluded.publish_status, "
                "metadata_json=excluded.metadata_json",
                (action_id, kind, url, "published" if published else "not_published", NOW, json.dumps(metadata)),
            )

    def _complete(self):
        with closing(database.connect(self.db_path)) as conn, conn:
            dispatch_delivery.complete(conn, [self.repo_id], NOW)

    def _notices(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT metadata_json FROM notifications WHERE notification_type = 'dispatch_completed'"
                )
            ]

    def _assert_pending(self):
        self._complete()
        self.assertEqual(self.command(self.request_id, "status")["status"], "admitted")
        self.assertEqual(self._notices(), [])

    def test_analysis_completes_with_issue_result_and_notifies_once(self):
        self._result("comment_analysis")
        self._action("comment", "comment", COMMENT)

        self._complete()
        self._complete()

        self.assertEqual(self.command(self.request_id, "status")["status"], "delivered")
        notices = self._notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["request_id"], self.request_id)
        self.assertEqual(notices[0]["issue_url"], ISSUE)
        self.assertEqual(notices[0]["result_url"], COMMENT)
        self.assertIsNone(notices[0]["pr_url"])

    def test_development_waits_for_confirmed_pr_and_original_issue_comment(self):
        self._result("new_pr")
        self._action("pr", "open_pr", PR)
        self._action("pr-comment", "comment", f"{PR}#issuecomment-41")
        self._assert_pending()

        self._action("issue-comment", "comment", COMMENT, published=False)
        self._assert_pending()

        self._action("issue-comment", "comment", COMMENT, receipt=False)
        self._assert_pending()

        self._action("issue-comment", "comment", COMMENT)
        self._complete()
        self.assertEqual(self.command(self.request_id, "status")["status"], "delivered")
        self.assertEqual([notice["pr_url"] for notice in self._notices()], [PR])

    def test_classification_question_and_running_task_are_not_delivery(self):
        self._result("classification_result")
        self._assert_pending()

        self._action("comment", "comment", COMMENT)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE worker_results SET output_type = 'waiting_for_user' WHERE result_id = 'result'")
        self._assert_pending()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE worker_results SET output_type = 'comment_analysis' WHERE result_id = 'result'")
            conn.execute("UPDATE tasks SET lifecycle = 'running' WHERE task_id = 'task'")
        self._assert_pending()

    def test_receipt_for_another_action_cannot_complete_request(self):
        self._result("comment_analysis")
        self._action("comment", "comment", COMMENT, receipt_action_id="different-comment")
        self._assert_pending()

    def test_development_requires_receipts_for_all_published_actions(self):
        self._result("new_pr")
        self._action("pr", "open_pr", PR)
        self._action("comment", "comment", COMMENT)
        self._action("push", "push_existing_pr", PR, receipt=False)
        self._assert_pending()

    def test_status_never_exposes_worker_handoff_secrets(self):
        self._result("comment_analysis")
        self._action("comment", "comment", COMMENT)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE worker_results SET handoff='password=supersecret'")
        self._complete()
        status = self.command(self.request_id, "status")
        self.assertEqual(status["status"], "delivered")
        self.assertNotIn("supersecret", json.dumps(status))
        self.assertNotIn("supersecret", json.dumps(self._notices()))

    def test_bare_issue_url_without_remote_comment_identity_is_not_delivery(self):
        self._result("comment_analysis")
        self._action("comment", "comment", ISSUE)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_remove(metadata_json,'$.publish.receipt.external_id')"
            )
        self._assert_pending()

    def test_status_redacts_before_truncating_at_credential_boundary(self):
        self._result("comment_analysis")
        self._action("comment", "comment", COMMENT)
        value = "SyntheticBoundaryCredential"
        line = f'\npassword="{value}'
        handoff = "x" * (2000 - len(line)) + line + '"'
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE worker_results SET handoff=?", (handoff,))
        self._complete()
        self.assertNotIn(value, json.dumps(self.command(self.request_id, "status")))

from contextlib import closing
import json
import sqlite3

from tests.support.summary_database import SummaryFixture


class AgentSessionHTTPTests(SummaryFixture):
    def setUp(self):
        super().setUp()
        self.db_path = self.root / "dd.sqlite3"
        self._init_summary_db(self.db_path)
        self.log = self.root / "task-artifacts/worker.stdout.log"
        self.log.write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "id": "agent",
                        "text": "Readable message",
                    },
                }
            )
            + "\n"
        )

    def request(self, query="task_id=task-1&attempt_id=attempt-1"):
        from robert_agent.interfaces.web.server import build_http_response

        status, headers, body = build_http_response("/api/agent-session?" + query, self.db_path)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        return status, json.loads(body)

    def test_read_only_endpoint_exposes_normalized_attempt_history(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            before = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        status, page = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(page["items"][0]["text"], "Readable message")
        self.assertEqual(page["items"][0]["kind"], "assistant")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(before, conn.execute("SELECT count(*) FROM task_events").fetchone()[0])

    def test_wrong_task_attempt_pair_returns_not_found(self):
        self.assertEqual(self.request("task_id=task-other&attempt_id=attempt-1")[0], 404)
        self.assertEqual(self.request("attempt_id=attempt-1")[0], 404)

    def test_limits_and_cursor_are_validated(self):
        for suffix in ("&limit=0", "&limit=51", "&limit=oops", "&cursor=../../etc/passwd"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request("task_id=task-1&attempt_id=attempt-1" + suffix)[0], 400)

    def test_missing_history_returns_empty_state(self):
        self.log.unlink()
        status, page = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(page["status"], "unavailable")
        self.assertEqual(page["items"], [])

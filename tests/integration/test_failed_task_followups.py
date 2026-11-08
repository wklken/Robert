"""Failure leaves attached followups visible without starting new work."""

from contextlib import closing
import json
import sqlite3
import unittest

from tests.support.terminal_github_followup import TerminalGithubFollowupFixture
from robert_agent.work.application import lifecycle
from robert_agent.foundation import values


class FailedTaskFollowupTests(unittest.TestCase):
    def test_failure_records_pending_followups_once(self):
        fixture = TerminalGithubFollowupFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.deliver("first")
        fixture.deliver("followup")
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            task, stream = conn.execute("SELECT task_id,workstream_id FROM tasks").fetchone()
            conn.execute(
                "UPDATE task_events SET relationship='pending' WHERE event_id IN (SELECT event_id FROM github_events WHERE event_fingerprint='comment:followup')"
            )
            for _ in range(2):
                lifecycle.finalize_failed_task(conn, task, stream, values.utc_now(), {"status": "failed_dispatch"})
            rows = conn.execute(
                "SELECT body,metadata_json FROM work_item_events WHERE json_extract(metadata_json,'$.kind')='pending_followups'"
            ).fetchall()
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM notifications n JOIN work_item_events e ON json_extract(n.metadata_json,'$.attention_event_id')=e.event_id WHERE json_extract(e.metadata_json,'$.kind')='pending_followups'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(len(rows), 1)
            self.assertIn("manual", rows[0][0].lower())
            self.assertIn("comment:followup", rows[0][0])
            self.assertIn("analyze caching", rows[0][0])
            self.assertEqual(json.loads(rows[0][1])["pending_event_fingerprints"], ["comment:followup"])
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed")

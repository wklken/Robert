"""Published analysis completes the board item, including older missing markers."""
from contextlib import closing
import json
import sqlite3
import unittest

from tests import test_board as fixtures
from robert_agent import board, run_once, work_items


class AnalysisCompletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BoardReadModelTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db_path
        self.now = "2026-10-27T03:43:18+00:00"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,state) VALUES ('source','repo-1','github:example/repo#119','issue',119,'open')")
            conn.execute("INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,created_at,updated_at) VALUES ('stream','repo-1','source','completed',?,?)", (self.now, self.now))
            self.item = work_items.ensure_github_work_item(
                conn, repo_id="repo-1", source_id="source", workstream_id="stream",
                actor_identity="owner", route_confidence="high", now=self.now,
            )
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,route_id,expected_output,created_at,updated_at) VALUES ('task','stream','completed','comment-analysis','comment_analysis',?,?)", (self.now, self.now))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('attempt','task',1,'completed')")
            conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES ('result','task','attempt','comment_analysis',?,?)", (self.now, json.dumps({"audit": {"status": "accepted"}})))
            conn.execute("INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at) VALUES ('action','result','task','comment','accepted','published',?)", (self.now,))

    def change(self, sql, args=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(sql, args)

    def reconcile(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            run_once._complete_published_analysis_items(conn, "repo-1", self.now)
        return board.get_work_item_detail(self.db, self.item["work_item_id"])

    def test_historical_published_analysis_is_completed_once(self):
        self.assertNotEqual(board.get_work_item_detail(self.db, self.item["work_item_id"])["column"], "done")
        item = self.reconcile()
        self.assertEqual(item["column"], "done")
        self.assertIsNotNone(item["completed_at"])
        self.assertEqual(item["valid_commands"], ["reopen"])
        replay = self.reconcile()
        self.assertEqual(replay["version"], item["version"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM work_item_events WHERE event_type='completed'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM github_actions").fetchone()[0], 1)

    def test_pending_failed_skipped_or_unaudited_publication_stays_unfinished(self):
        for audit, publication in [("accepted", "not_published"), ("failed", "published"), ("accepted", "skipped")]:
            with self.subTest(audit=audit, publication=publication):
                self.change("UPDATE github_actions SET audit_status=?,publish_status=?", (audit, publication))
                self.assertIsNone(self.reconcile()["completed_at"])
        self.change("UPDATE github_actions SET audit_status='accepted',publish_status='published'")
        self.change("UPDATE worker_results SET metadata_json='{}'")
        self.assertIsNone(self.reconcile()["completed_at"])

    def test_missing_comment_is_not_a_published_analysis(self):
        self.change("DELETE FROM github_actions")
        self.assertIsNone(self.reconcile()["completed_at"])

    def test_pending_request_is_not_hidden_by_completion(self):
        self.change("INSERT INTO github_events(event_id,repo_id,source_id,event_fingerprint,event_type,event_at) VALUES ('event','repo-1','source','comment:next','comment',?)", (self.now,))
        self.change("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES ('task','event','pending',?)", (self.now,))
        self.assertIsNone(self.reconcile()["completed_at"])

    def test_newer_task_is_not_hidden_by_old_analysis(self):
        self.change("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('new-task','stream','queued','2026-10-27T04:00:00Z','2026-10-27T04:00:00Z')")
        self.assertIsNone(self.reconcile()["completed_at"])

    def test_running_worker_prevents_completion(self):
        self.change("UPDATE attempts SET status='running'")
        self.assertIsNone(self.reconcile()["completed_at"])

    def test_operator_question_prevents_completion(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            work_items.record_system_event(conn, self.item["work_item_id"], event_type="operator_question", idempotency_key="question", body="Choose the next step", now=self.now)
        self.assertIsNone(self.reconcile()["completed_at"])

    def test_related_pr_keeps_its_existing_lifecycle(self):
        self.change("INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,state) VALUES ('pr','repo-1','github:example/repo!120','pull_request',120,'open')")
        self.change("INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,lifecycle,created_at,updated_at) VALUES ('pr-stream','repo-1','pr','stream','completed',?,?)", (self.now,self.now))
        self.assertEqual(self.reconcile()["column"], "review")

    def test_canceled_item_is_not_completed(self):
        self.change("UPDATE work_items SET canceled_at=?", (self.now,))
        self.assertIsNone(self.reconcile()["completed_at"])

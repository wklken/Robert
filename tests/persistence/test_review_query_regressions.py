"""Review regressions exercise query membership, not SQL spelling."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.queries import github_actions, notifications, tasks, workstreams
from robert_agent.persistence.stores import work_items
from robert_agent.persistence.stores import tasks as task_store

NOW = "2026-11-08T12:00:00+00:00"


class ReviewQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db = Path(temporary.name) / "state.db"
        init_database(db)
        self.conn = sqlite3.connect(db)
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('r','x/y','bot','main','/repo','/trees')"
        )
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,created_at,updated_at) VALUES('w','r','s',?,?)",
            (NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('t','w','queued','2026-11-08T11:00:00+00:00',?)",
            (NOW,),
        )
        self.conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('a','t',1,'completed')")
        self.conn.execute(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES('result','t','a','new_pr',?,'{\"audit\":{\"status\":\"accepted\"}}')",
            (NOW,),
        )

    def action(self, name, kind="comment", status="not_published"):
        self.conn.execute(
            "INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at) VALUES(?,'result','t',?,'accepted',?,?)",
            (name, kind, status, NOW),
        )

    def test_fresh_publication_does_not_misclassify_old_queue(self):
        self.action("fresh")
        rows = tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)
        self.assertEqual(rows[0][1], "queue_delayed")
        self.conn.execute("UPDATE github_actions SET created_at='2026-11-08T11:00:00+00:00'")
        self.assertEqual(tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)[0][1], "publication_delayed")

    def test_active_attempt_is_not_reported_as_queue_delay(self):
        self.conn.execute("DELETE FROM worker_results")
        for status in ("running", "stale"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE attempts SET status=?,heartbeat_at=?", (status, NOW))
                self.assertEqual(list(tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)), [])

    def test_prepared_attempt_still_reports_queue_delay(self):
        self.conn.execute("DELETE FROM worker_results")
        self.conn.execute("UPDATE attempts SET status='prepared'")
        self.assertEqual(
            [tuple(row) for row in tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)],
            [("t", "queue_delayed")],
        )

    def test_launch_inflight_and_unconfirmed_exit_are_not_queue_delays(self):
        self.conn.execute("DELETE FROM worker_results")
        self.conn.execute("UPDATE attempts SET status='completed',metadata_json='{\"dispatch\":{\"pid\":123}}'")
        self.assertEqual(list(tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)), [])
        self.conn.execute("UPDATE attempts SET status='prepared',metadata_json='{}'")
        self.conn.execute(
            "INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at,metadata_json) "
            "VALUES('task','t','human_request','active',?,?, '{\"dispatch_inflight\":\"a\"}')",
            (NOW, NOW),
        )
        self.assertEqual(list(tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)), [])

    def test_active_attempt_does_not_hide_publication_delay(self):
        self.conn.execute("UPDATE attempts SET status='running'")
        self.action("delayed")
        self.conn.execute("UPDATE github_actions SET created_at='2026-11-08T11:00:00+00:00'")
        self.assertEqual(
            [tuple(row) for row in tasks.collect_filtered(self.conn, ["r"], NOW, NOW, 10)],
            [("t", "publication_delayed")],
        )

    def test_dispatch_start_only_promotes_queued_task_for_its_running_attempt(self):
        self.conn.execute("UPDATE attempts SET status='running'")
        task_store.mark_task_running_for_attempt(self.conn, "t", "a", NOW)
        self.assertEqual(self.conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "running")
        for lifecycle in ("completed", "canceled", "failed", "waiting_for_user"):
            with self.subTest(lifecycle=lifecycle):
                self.conn.execute("UPDATE tasks SET lifecycle=?", (lifecycle,))
                task_store.mark_task_running_for_attempt(self.conn, "t", "a", NOW)
                self.assertEqual(self.conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], lifecycle)
        self.conn.execute("UPDATE tasks SET lifecycle='queued'")
        task_store.mark_task_running_for_attempt(self.conn, "t", "missing", NOW)
        self.assertEqual(self.conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "queued")
        self.conn.execute("UPDATE attempts SET status='completed'")
        task_store.mark_task_running_for_attempt(self.conn, "t", "a", NOW)
        self.assertEqual(self.conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "queued")

    def test_skipped_publication_does_not_block_future_source_work(self):
        self.conn.execute("UPDATE tasks SET lifecycle='completed'")
        self.action("skipped", status="skipped")
        self.assertFalse(workstreams.find_for_source_has_active_unpublished_result(self.conn, "s", "s"))
        self.conn.execute("UPDATE github_actions SET publish_status='not_published'")
        self.assertTrue(workstreams.find_for_source_has_active_unpublished_result(self.conn, "s", "s"))

    def test_delivery_receipts_are_not_hidden_by_earlier_comments(self):
        for index, kind in enumerate(["comment"] * 5 + ["push_existing_pr", "open_pr"]):
            name = f"a{index}"
            self.action(name, kind, "published")
            self.conn.execute(
                "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at) VALUES(?,?,?,?,'published',?,?)",
                (name, name, name, name, NOW, NOW),
            )
        self.assertEqual(
            {row[1] for row in github_actions.find_for_delivery(self.conn, "result")}, {"push_existing_pr", "open_pr"}
        )

    def test_readiness_and_delivery_share_empty_and_omitted_scope(self):
        self.conn.execute(
            "INSERT INTO notifications(notification_id,task_id,notification_type,status,created_at) VALUES('n','t','test','recorded',?)",
            (NOW,),
        )
        for scope, expected in ((None, True), ([], False), (["r"], True), (["other"], False)):
            with self.subTest(scope=scope):
                self.assertEqual(bool(notifications.find_for_deliver_pending(self.conn, NOW, 10, scope)), expected)
                self.assertEqual(
                    bool(notifications.has_pending_local_delivery_filtered(self.conn, scope, NOW)), expected
                )

    def test_nonexistent_route_field_is_rejected_before_sql(self):
        with self.assertRaises(ValueError):
            work_items.edit_fields(self.conn, "missing", {"route_id": "new-pr"}, NOW)

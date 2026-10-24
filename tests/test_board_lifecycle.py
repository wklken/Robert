"""Contracts for the operator's lifecycle board and its advertised actions."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_board as board_fixtures
from robert_agent import board, publish, web, work_items


class BoardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = board_fixtures.BoardReadModelTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db_path
        self.context = self.fixture.context

    def create(self, title="Task", **kwargs):
        return self.fixture._create(title, **kwargs)

    def command(self, item, command, **kwargs):
        return work_items.execute_command(
            self.db, item["work_item_id"], context=self.context, command=command,
            expected_version=item["version"], idempotency_key=f"test-{command}", **kwargs,
        )

    def event(self, item, kind, **kwargs):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return work_items.record_system_event(
                conn, item["work_item_id"], event_type=kind,
                idempotency_key=f"event-{kind}", **kwargs,
            )

    def test_home_is_board_and_legacy_list_remains_accessible(self):
        self.assertIn(b'id="board-app"', web.build_http_response("/", self.db)[2])
        self.assertIn(b'id="workbench-app"', web.build_http_response("/work", self.db)[2])

    def test_execution_history_identifies_missing_logs_per_attempt(self):
        item = self.fixture._seed_six_columns()["doing"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM attempts WHERE attempt_id='attempt-doing'").fetchone()[0]
            conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at) VALUES (?,?,?,?,?,?)",
                ('log-old', task_id, 'attempt-doing', 'worker_stderr', '/missing-remote-data/attempt.log', '2026-10-22'),
            )
        detail = board.get_work_item_detail(self.db, item['work_item_id'])
        log = detail['tasks'][0]['attempts'][0]['artifacts'][0]
        self.assertEqual(log['artifact_id'], 'log-old')
        self.assertEqual(log['artifact_type'], 'worker_stderr')
        self.assertFalse(log['available'])
        self.assertNotIn('path', log)

    def test_execution_reports_phase_heartbeat_and_safe_failure(self):
        items = self.fixture._seed_six_columns()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET heartbeat_at = '2026-10-22T00:02:00+00:00', failure_json = ? WHERE attempt_id = 'attempt-doing'", (json.dumps({"safe_error": "Tests failed in /home/operator/private", "stderr": "do not expose"}),))
            conn.execute("INSERT INTO worker_phases(phase_id, attempt_id, phase, status, summary, next_step, created_at) VALUES ('phase', 'attempt-doing', 'verify', 'running', 'Checking regressions', 'Read results', '2026-10-22T00:02:00+00:00')")
        detail = board.get_work_item_detail(self.db, items["doing"]["work_item_id"])
        execution = detail["execution"]
        self.assertEqual(execution["phase"], "verify")
        self.assertEqual(execution["summary"], "Checking regressions")
        self.assertEqual(execution["heartbeat_at"], "2026-10-22T00:02:00+00:00")
        self.assertEqual(execution["next_step"], "Read results")
        self.assertIn("<local-path>", execution["failure"])
        self.assertNotIn("do not expose", json.dumps(detail))
        self.assertNotIn("/home/operator/private", json.dumps(detail))

    def test_review_failure_offers_retry(self):
        items = self.fixture._seed_six_columns()
        self.event(items["review"], "publication_failed", body="Publication rejected")
        detail = board.get_work_item_detail(self.db, items["review"]["work_item_id"])
        self.assertIn("retry", detail["valid_commands"])
        result = self.command(detail, "retry")
        self.assertTrue(result["ok"])

    def test_retry_targets_failure_even_with_newer_question(self):
        item = self.create(start=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE workstream_id=?", (item["workstream_id"],))
        failure = self.event(item, "execution_failed", now="2026-10-22T00:00:00+00:00")
        self.event(item, "operator_question", now="2026-10-22T00:01:00+00:00")
        result = self.command(item, "retry")
        self.assertEqual(result["resolved_event_id"], failure["event_id"])

    def test_canceled_item_rejects_stale_retry(self):
        item = self.create(start=True)
        self.event(item, "execution_failed")
        canceled = self.command(item, "cancel")["item"]
        with self.assertRaises(work_items.WorkItemConflictError):
            self.command(canceled, "retry")
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertEqual(detail["valid_commands"], [])
        self.assertEqual(board.list_board(self.db, include_history=True)["counts"]["history"], 1)

    def test_verified_running_worker_can_be_canceled_and_replayed(self):
        item = self.fixture._seed_six_columns()["doing"]
        with patch("robert_agent.run_once._terminate_attempt_process", return_value={"status": "terminated", "signal": "SIGTERM"}) as terminate:
            result = self.command(item, "cancel")
            self.assertTrue(result["item"]["canceled_at"])
            self.command(item, "cancel")
            terminate.assert_called_once()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE attempt_id='attempt-doing'").fetchone()[0], "canceled")
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks WHERE task_id=(SELECT task_id FROM attempts WHERE attempt_id='attempt-doing')").fetchone()[0], "canceled")

    def test_failed_identity_verification_does_not_cancel(self):
        item = self.fixture._seed_six_columns()["doing"]
        with patch("robert_agent.run_once._terminate_attempt_process", return_value={"status": "not_signalled", "reason": "missing_pid"}):
            with self.assertRaisesRegex(work_items.WorkItemConflictError, "missing_pid"):
                self.command(item, "cancel")
        self.assertIsNone(board.get_work_item_detail(self.db, item["work_item_id"])["canceled_at"])

    def test_active_attempt_blocks_duplicate_followup(self):
        item = self.fixture._seed_six_columns()["doing"]
        self.event(item, "execution_failed")
        with self.assertRaises(work_items.WorkItemConflictError):
            self.command(item, "retry")
        self.assertNotIn("retry", board.get_work_item_detail(self.db, item["work_item_id"])["valid_commands"])

    def seed_publication(self, item, failed=True, audited=True):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task_id = conn.execute("SELECT active_task_id FROM workstreams WHERE workstream_id=?", (item["workstream_id"],)).fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (task_id,))
            metadata = {"publish": {"status": "publish_failed", "safe_error": "GitHub permission denied in /home/operator/private"}} if failed else {}
            metadata.update({'body':'Done <!-- robert-comment test -->'})
            if audited:
                conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('publication-attempt',?,1,'completed')", (task_id,))
                scope = {'repo':'example/repo', 'sources':[{'source_type':'issue','number':1}]}
                conn.execute("INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES ('result',?,'publication-attempt','comment_analysis','2026-10-22',?)", (task_id,json.dumps({'audit':{'status':'accepted','action_scope':scope}})))
            conn.execute("INSERT INTO github_actions(action_id,task_id,action_type,audit_status,created_at,metadata_json) VALUES ('publish-action',?,'comment','accepted','2026-10-22',?)", (task_id, json.dumps(metadata)))
            if audited:
                conn.execute("UPDATE github_actions SET result_id='result', target_url='https://github.com/example/repo/issues/1'")
        return task_id

    def test_publication_retry_reuses_result_and_blocks_duplicate_worker(self):
        item = self.create(start=True)
        task_id = self.seed_publication(item)
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertEqual(detail["column"], "waiting")
        self.assertEqual(detail["valid_commands"], ["retry_publish", "cancel"])
        self.assertNotIn("/home/operator/private", json.dumps(detail))
        with self.assertRaises(work_items.WorkItemConflictError):
            self.command(item, "retry")
        result = self.command(item, "retry_publish")
        self.assertEqual(result["task_id"], task_id)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM wakeups WHERE json_extract(metadata_json,'$.kind')='publication_retry'").fetchone()[0], 1)
        self.assertEqual(board.get_work_item_detail(self.db, item["work_item_id"])["valid_commands"], ["cancel"])
        self.assertEqual(self.command(item, "retry_publish"), result)

    def test_task_history_exposes_publication_evidence_without_raw_metadata(self):
        item = self.create(start=True)
        self.seed_publication(item)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET metadata_json=?", (json.dumps({
                "body": "private payload", "worktree_path": "/home/private/repo",
                "publish": {"status": "publish_failed", "safe_error": "Denied in /home/private/repo", "response": {"stdout": "raw secret"}},
            }),))
        action = board.get_work_item_detail(self.db, item['work_item_id'])['tasks'][0]['publication_actions'][0]
        self.assertEqual(action['action_type'], 'comment')
        self.assertEqual(action['status'], 'publish_failed')
        self.assertIn('<local-path>', action['safe_error'])
        self.assertNotIn('private payload', json.dumps(action))
        self.assertNotIn('raw secret', json.dumps(action))
        self.assertNotIn('/home/private', json.dumps(action))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status='published', metadata_json=?", (json.dumps({
                "publish": {"published_at": "2026-10-22T03:14:14Z", "status": "published"},
            }),))
        action = board.get_work_item_detail(self.db, item['work_item_id'])['tasks'][0]['publication_actions'][0]
        self.assertEqual(action['status'], 'published')
        self.assertEqual(action['published_at'], '2026-10-22T03:14:14Z')

    def test_cancel_prevents_queued_publication(self):
        item = self.create(start=True)
        self.seed_publication(item, failed=False)
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertIn("cancel", detail["valid_commands"])
        self.command(item, "cancel")
        with patch("robert_agent.publish._publish_action") as publisher:
            result = publish.publish_ready_actions(self.db)
        publisher.assert_not_called()
        self.assertEqual(result["pending_count"], 0)

    def test_queued_retry_does_not_offer_unusable_old_attention_actions(self):
        item = self.create(start=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='failed'")
        self.event(item, "execution_failed", now="2026-10-22T00:00:00+00:00")
        self.event(item, "manual_worker_unavailable", now="2026-10-22T00:01:00+00:00")
        self.command(item, "retry")
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertEqual(detail["column"], "todo")
        self.assertEqual(detail["valid_commands"], ["cancel"])

    def test_closed_child_pr_exposes_its_running_worker(self):
        item = self.fixture._seed_six_columns()["review"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_sources SET state='closed' WHERE source_id='pr-review'")
            conn.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('child-task','github:example/repo!12','running','2026-10-22','2026-10-22')")
            conn.execute("UPDATE workstreams SET active_task_id='child-task', lifecycle='active' WHERE workstream_id='github:example/repo!12'")
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,metadata_json) VALUES ('child-attempt','child-task',1,'running','2026-10-22','{}')")
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertEqual(detail["reason_code"], "closed_source_running")
        self.assertEqual(detail["execution"]["attempt_id"], "child-attempt")
        self.assertEqual(detail["valid_commands"], ["cancel"])
        self.assertIn("child-task", [task["task_id"] for task in detail["tasks"]])
        with patch("robert_agent.run_once._terminate_attempt_process", return_value={"status": "not_found"}):
            self.command(item, "cancel")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks WHERE task_id='child-task'").fetchone()[0], "canceled")

    def test_incomplete_cleanup_is_visible_before_source_state_updates(self):
        item = self.fixture._seed_six_columns()["doing"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id='attempt-doing'", (json.dumps({"termination": {"status": "permission_denied"}}),))
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertEqual(detail["column"], "waiting")
        self.assertEqual(detail["reason_code"], "worker_cleanup_pending")
        self.assertEqual(detail["execution"]["cleanup_reason"], "permission_denied")

    def test_repository_cycle_prevents_racing_cancellation(self):
        item = self.fixture._seed_six_columns()["doing"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO leases(lease_id,resource_type,resource_key,owner_id,status,acquired_at,expires_at,heartbeat_at) VALUES ('lease','agent_run','repo-1','engine','active','2026-10-22','9999-01-01','2026-10-22')")
        with patch("robert_agent.run_once._terminate_attempt_process") as terminate:
            with self.assertRaisesRegex(work_items.WorkItemConflictError, "processing a cycle"):
                self.command(item, "cancel")
        terminate.assert_not_called()

    def test_failed_publication_can_be_retried_again_after_attempt_finishes(self):
        item = self.create(start=True)
        self.seed_publication(item)
        self.command(item, "retry_publish")
        with patch("robert_agent.publish._publish_action", return_value={"status": "publish_failed", "safe_error": "Offline"}):
            publish.publish_ready_actions(self.db)
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertIn("retry_publish", detail["valid_commands"])
        self.assertFalse(detail["publication"]["retry_pending"])

    def test_failed_task_without_event_remains_recoverable(self):
        item = self.create(start=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='failed'")
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertIn("retry", detail["valid_commands"])
        self.command(item, "retry")
        self.assertEqual(board.get_work_item_detail(self.db, item["work_item_id"])["column"], "todo")

    def test_manual_unavailable_prepared_task_can_change_worker(self):
        item = self.create(start=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='queued',routing_mode='manual',requested_worker='removed-worker'")
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at) VALUES ('prepared',?,1,'prepared','2026-10-22')", (task,))
        self.event(item, "manual_worker_unavailable", metadata={"task_id": task})
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertEqual(detail["column"], "waiting")
        self.assertEqual(detail["valid_commands"], ["retry", "cancel"])
        result = self.command(item, "retry", routing_mode="auto")
        self.assertNotEqual(result["task_id"], task)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts WHERE attempt_id='prepared'").fetchone()[0], "canceled")
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "canceled")
        self.assertEqual(board.get_work_item_detail(self.db, item["work_item_id"])["valid_commands"], ["cancel"])

    def test_external_publish_does_not_lock_database_but_blocks_same_task_cancel(self):
        item = self.create(start=True)
        self.seed_publication(item)
        def external_write(*args, **kwargs):
            with closing(sqlite3.connect(self.db, timeout=0)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            with self.assertRaisesRegex(work_items.WorkItemConflictError, "publication"):
                self.command(item, "cancel")
            return {"status": "published", "ok": True}
        with patch("robert_agent.publish._publish_action", side_effect=external_write):
            self.assertTrue(publish.publish_ready_actions(self.db)["ok"])

    def test_publication_timeout_is_retryable(self):
        import subprocess
        item = self.create(start=True)
        self.seed_publication(item)
        with patch("robert_agent.publish._publish_action", side_effect=subprocess.TimeoutExpired("gh", 60)):
            result = publish.publish_ready_actions(self.db)
        self.assertEqual(result["failed_count"], 1)
        detail = board.get_work_item_detail(self.db, item["work_item_id"])
        self.assertIn("retry_publish", detail["valid_commands"])
        self.assertIn("timed out", detail["publication"]["failures"][0]["reason"])

    def test_reply_rejects_a_different_question_even_at_the_same_version(self):
        item = self.fixture._seed_six_columns()["waiting"]
        old = board.get_work_item_detail(self.db, item["work_item_id"])["attention_signals"][0]["event_id"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            work_items.record_system_event(conn, item["work_item_id"], event_type="operator_question", idempotency_key="new-question", body="New question")
        with self.assertRaisesRegex(work_items.WorkItemConflictError, "attention"):
            self.command(item, "reply", body="Answer to the old question", expected_event_id=old)

    def test_busy_publisher_cannot_skip_failed_push_and_publish_its_comment(self):
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import contextmanager
        import threading
        from robert_agent.publication_lock import task_publication_lock
        item = self.create(start=True)
        self.seed_publication(item)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE github_actions SET action_id='a-push',result_id='result'")
            conn.execute("INSERT INTO github_actions(action_id,task_id,result_id,action_type,audit_status,publish_status,created_at,metadata_json) SELECT 'b-comment',task_id,'result','comment','accepted','not_published',created_at,'{}' FROM github_actions WHERE action_id='a-push'")
        started, skipped, done = threading.Event(), threading.Event(), threading.Event()
        calls = []
        @contextmanager
        def controlled_lock(db, task):
            with task_publication_lock(db, task) as acquired:
                yield acquired
            if threading.current_thread().name == "publisher-B" and not acquired:
                skipped.set()
                self.assertTrue(done.wait(3))
        def external(action, *args, **kwargs):
            calls.append((threading.current_thread().name, action["action_id"]))
            if action["action_id"] == "a-push":
                started.set()
                self.assertTrue(skipped.wait(3))
                return {"ok": False, "status": "publish_failed", "safe_error": "push failed"}
            return {"ok": True, "status": "published"}
        def runner(name):
            threading.current_thread().name = name
            try:
                return publish.publish_ready_actions(self.db)
            finally:
                if name == "publisher-A":
                    done.set()
        with patch("robert_agent.publish.task_publication_lock", side_effect=controlled_lock), patch("robert_agent.publish._publish_action", side_effect=external):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(runner, "publisher-A")
                self.assertTrue(started.wait(3))
                second = pool.submit(runner, "publisher-B")
                self.assertEqual(first.result(timeout=5)["failed_count"], 1)
                self.assertEqual(second.result(timeout=5)["published_count"], 0)
        self.assertEqual(calls, [("publisher-A", "a-push")])


    def test_execution_history_keeps_each_attempt_and_its_safe_phases(self):
        item = self.fixture._seed_six_columns()["doing"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM attempts WHERE attempt_id='attempt-doing'").fetchone()[0]
            conn.execute("UPDATE attempts SET status='failed', failure_json=? WHERE attempt_id='attempt-doing'", (json.dumps({"safe_error": "Failed in /home/operator/private", "stderr": "hidden-output"}),))
            conn.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('retry-attempt',?,2,'running')", (task_id,))
            conn.execute("INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,next_step,created_at) VALUES ('old-phase','attempt-doing','verify','failed','Tests failed','Retry','2026-10-22')")
        detail = board.get_work_item_detail(self.db, item['work_item_id'])
        attempts = detail['tasks'][0]['attempts']
        self.assertEqual([a['attempt_no'] for a in attempts], [1, 2])
        self.assertIn('<local-path>', attempts[0]['failure'])
        self.assertEqual(attempts[0]['phases'][0]['summary'], 'Tests failed')
        self.assertNotIn('hidden-output', json.dumps(detail))

    def test_cancel_accepts_verified_dead_worker_and_closes_its_binding(self):
        from robert_agent import execution_store
        item = self.fixture._seed_six_columns()["doing"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM attempts WHERE attempt_id='attempt-doing'").fetchone()[0]
            execution_store.ensure_subject(conn, 'task', task_id, 'human_request', '2026-10-22')
        with patch('robert_agent.run_once._terminate_attempt_process', return_value={'status':'already_dead'}):
            self.command(item, 'cancel')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT binding_status FROM execution_subjects').fetchone()[0], 'terminal')

    def test_publication_scope_failure_is_visible_and_can_be_canceled(self):
        item = self.create(start=True)
        self.seed_publication(item, failed=False, audited=False)
        publish.publish_ready_actions(self.db)
        detail = board.get_work_item_detail(self.db, item['work_item_id'])
        self.assertEqual(detail['column'], 'waiting')
        self.assertIn('audited_scope_missing', json.dumps(detail['publication']['failures']))
        self.assertIn('cancel', detail['valid_commands'])

    def test_terminal_publication_attention_offers_cancel_without_false_retry(self):
        item = self.create(start=True)
        self.seed_publication(item)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at) VALUES ('intent','publish-action','intent-key','fingerprint','attention','now','now')")
        detail = board.get_work_item_detail(self.db, item['work_item_id'])
        self.assertEqual(detail['valid_commands'], ['cancel'])
        self.assertTrue(detail['publication']['blocked_reason'])
        with self.assertRaises(work_items.WorkItemConflictError):
            self.command(item, 'retry_publish')

    def test_recovery_quota_attention_is_visible_while_attempt_is_retained(self):
        item = self.fixture._seed_six_columns()['doing']
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=? WHERE attempt_id='attempt-doing'", (json.dumps({'supervise':{'status':'worker_identity_attention','recovery_decision':'attention'}}),))
        detail = board.get_work_item_detail(self.db, item['work_item_id'])
        self.assertEqual(detail['column'], 'waiting')
        self.assertEqual(detail['valid_commands'], ['cancel'])

    def test_old_database_readonly_inspection_and_mixed_controls_do_not_write(self):
        from pathlib import Path
        from robert_agent import execution_store, workbench
        item=self.create('Historical task')
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute('DROP TABLE execution_subjects')
        before=Path(self.db).read_bytes()
        board.get_work_item_detail(self.db,item['work_item_id'])
        workbench.list_work_items(self.db)
        self.assertEqual(Path(self.db).read_bytes(),before)
        with closing(sqlite3.connect(self.db)) as conn,conn:
            execution_store.initialize(conn)
            conn.execute("INSERT INTO workflow_bindings(subject_type,subject_id,workflow_kind,owner_engine,binding_status,workflow_id,created_at,updated_at) VALUES ('task','historical','human_request','dbos','attention','old:id','now','now')")
        before=Path(self.db).read_bytes()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(execution_store.read_binding(conn,'task','historical')['binding_status'],'attention')
        self.assertEqual(Path(self.db).read_bytes(),before)


if __name__ == "__main__":
    unittest.main()

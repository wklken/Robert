"""Delivered analysis is an idle delivery, never evidence of implementation failure."""

from contextlib import closing
import json
import sqlite3

from robert_agent.readmodels import board
from robert_agent.work.application import commands
from tests.support.board import BoardReadModelFixture


class BoardAnalysisDeliveryTests(BoardReadModelFixture):
    def setUp(self):
        super().setUp()
        self.now = "2026-11-24T01:00:00+00:00"
        self.url = "https://github.com/example/repo/issues/119#issuecomment-123"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,state,html_url) "
                "VALUES ('source','repo-1','github:example/repo#119','issue',119,'open',?)",
                (self.url,),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,lifecycle,created_at,updated_at) "
                "VALUES ('stream','repo-1','source','completed',?,?)",
                (self.now, self.now),
            )
            self.item = commands.ensure_github_work_item(
                conn,
                repo_id="repo-1",
                source_id="source",
                workstream_id="stream",
                actor_identity="owner",
                route_confidence="high",
                now=self.now,
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,route_id,expected_output,created_at,updated_at) "
                "VALUES ('task','stream','completed','comment-analysis','comment_analysis',?,?)",
                (self.now, self.now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('attempt','task',1,'completed')"
            )
            conn.execute(
                "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) "
                "VALUES ('result','task','attempt','comment_analysis',?,?)",
                (self.now, json.dumps({"audit": {"status": "accepted"}})),
            )
            conn.execute(
                "INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,"
                "target_url,external_id,metadata_json,created_at) VALUES ('action','result','task','comment',"
                "'accepted','published',?,'123',?,?)",
                (self.url, json.dumps({"publish": {"status": "published", "published_at": self.now}}), self.now),
            )

    def change(self, sql, args=()):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(sql, args)

    def detail(self):
        return board.get_work_item_detail(self.db_path, self.item["work_item_id"])

    def test_published_analysis_is_delivered_without_writing_completion(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            before = list(conn.iterdump())
        detail = self.detail()
        overview = board.list_board(self.db_path)
        self.assertEqual(detail["reason_code"], "analysis_delivered")
        self.assertEqual(detail["column"], "review")
        self.assertEqual(detail["status_summary"]["state"], "analysis_delivered")
        self.assertFalse(detail["status_summary"]["needs_action"])
        self.assertEqual(detail["status_summary"]["primary"], "open_source")
        self.assertEqual(detail["valid_commands"], ["cancel"])
        self.assertIsNone(detail["completed_at"])
        self.assertEqual(overview["counts"]["needs_action"], 0)
        self.assertEqual(overview["attention_items"], [])
        self.assertEqual(board.list_board(self.db_path, attention="true")["items"], [])
        proof = detail["publication"]["delivery"]
        self.assertEqual(proof["result_id"], "result")
        self.assertEqual(proof["attempt_id"], "attempt")
        self.assertEqual(proof["receipts"][0]["url"], self.url)
        self.assertEqual(proof["receipts"][0]["published_at"], self.now)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_tapd_analysis_uses_the_same_publication_proof(self):
        url = "https://tapd.woa.com/69995498/prong/stories/view/1169995498001000016"
        self.change(
            "UPDATE github_sources SET provider='tapd',source_type='tapd_story',number=NULL,"
            "project_key='69995498',external_id='1169995498001000016',html_url=?",
            (url,),
        )
        self.change("UPDATE work_items SET origin_type='tapd'")
        self.change("UPDATE github_actions SET target_url=?", (url,))
        detail = self.detail()
        self.assertEqual(detail["reason_code"], "analysis_delivered")
        self.assertFalse(detail["status_summary"]["needs_action"])
        self.assertEqual(detail["source"]["type"], "tapd_story")
        self.assertEqual(detail["publication"]["delivery"]["receipts"][0]["url"], url)

    def test_missing_unaccepted_skipped_or_pending_actions_are_not_delivery(self):
        for audit, status in [
            ("pending", "published"),
            ("failed", "published"),
            ("accepted", "not_published"),
            ("accepted", "skipped"),
        ]:
            with self.subTest(audit=audit, status=status):
                self.change("UPDATE github_actions SET audit_status=?,publish_status=?", (audit, status))
                self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
        self.change("DELETE FROM github_actions")
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_unsettled_publication_intent_is_not_delivery(self):
        self.change(
            "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,"
            "created_at,updated_at) VALUES ('intent','action','key','fingerprint','attention',?,?)",
            (self.now, self.now),
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
        self.change("UPDATE publication_intents SET status='published'")
        self.assertEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_one_published_comment_does_not_hide_another_unsettled_action(self):
        self.change(
            "INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at) "
            "VALUES ('second','result','task','comment','accepted','not_published',?)",
            (self.now,),
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
        self.change("UPDATE github_actions SET publish_status='published' WHERE action_id='second'")
        self.assertEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_new_request_and_live_native_slot_prevent_delivery(self):
        self.change(
            "INSERT INTO wakeups(wakeup_id,repo_id,reason,dedupe_key,task_id,status,not_before_at,created_at,updated_at) "
            "VALUES ('wakeup','repo-1','manual_operator_request','next','task','pending',?,?,?)",
            (self.now, self.now, self.now),
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
        self.change("DELETE FROM wakeups")
        self.change(
            "INSERT INTO pre_pr_review_sessions(round_id,task_id,status,request_json,created_at,updated_at) "
            "VALUES ('round','task','running','{}',?,?)",
            (self.now, self.now),
        )
        self.change(
            "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,"
            "output_dir,prompt_sha256,slot_reserved,created_at,updated_at) "
            "VALUES ('run','round','review','reviewer',1,'attempt','running','/unused','sha',1,?,?)",
            (self.now, self.now),
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_receipts_redact_private_fields_and_refuse_unsafe_links(self):
        self.change(
            "UPDATE github_actions SET target_url='javascript:alert(1)',metadata_json=?",
            (json.dumps({"body": "PRIVATE REPORT BODY", "publish": {"status": "published"}}),),
        )
        detail = self.detail()
        self.assertIsNone(detail["publication"]["delivery"]["receipts"][0]["url"])
        self.assertNotIn("PRIVATE REPORT BODY", json.dumps(detail))
        self.assertNotIn("javascript:", json.dumps(detail))

    def test_latest_result_attempt_and_audit_must_match(self):
        for sql in [
            "UPDATE worker_results SET metadata_json='{}'",
            "UPDATE worker_results SET output_type='waiting_for_user'",
            "UPDATE attempts SET status='failed'",
            "UPDATE tasks SET lifecycle='failed'",
        ]:
            with self.subTest(sql=sql):
                self.change(sql)
                self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
                self.change(
                    "UPDATE worker_results SET output_type='comment_analysis',metadata_json=?",
                    (json.dumps({"audit": {"status": "accepted"}}),),
                )
                self.change("UPDATE attempts SET status='completed'")
                self.change("UPDATE tasks SET lifecycle='completed'")
        self.change("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('new','task',2,'failed')")
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
        self.change("UPDATE attempts SET status='completed' WHERE attempt_id='new'")
        self.change(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) "
            "VALUES ('new','task','new','comment_analysis','2026-11-24T02:00:00Z','{}')"
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_pending_inputs_and_newer_tasks_are_not_hidden(self):
        self.change(
            "INSERT INTO github_events(event_id,repo_id,source_id,event_fingerprint,event_type,event_at) "
            "VALUES ('event','repo-1','source','next','comment',?)",
            (self.now,),
        )
        for relationship in ("pending", "context"):
            self.change(
                "INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES ('task','event',?,?)",
                (relationship, self.now),
            )
            self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
            self.change("DELETE FROM task_events")
        self.change(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
            "VALUES ('new','stream','queued','2026-11-24T02:00:00Z','2026-11-24T02:00:00Z')"
        )
        self.assertEqual(self.detail()["column"], "todo")

    def test_outstanding_implementation_is_not_a_delivered_analysis(self):
        self.change(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,route_id,created_at,updated_at) "
            "VALUES ('parent','stream','completed','new-pr','2026-11-24T00:00:00Z','2026-11-24T00:00:00Z')"
        )
        self.change("UPDATE tasks SET parent_task_id='parent' WHERE task_id='task'")
        self.change(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('parent','parent',1,'completed')"
        )
        self.change(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) "
            "VALUES ('parent','parent','parent','waiting_for_user',?,?)",
            (self.now, json.dumps({"audit": {"status": "accepted"}})),
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_question_failure_running_and_cleanup_keep_attention(self):
        for event in ("operator_question", "execution_failed", "operator_decision_required"):
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                commands.record_system_event(
                    conn,
                    self.item["work_item_id"],
                    event_type=event,
                    idempotency_key=event,
                    body="Actual outstanding work",
                    now=self.now,
                )
            self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")
            self.change("DELETE FROM work_item_events WHERE event_type=?", (event,))
        self.change("UPDATE attempts SET status='running'")
        self.assertEqual(self.detail()["column"], "doing")
        self.change(
            "UPDATE attempts SET status='completed',metadata_json=?",
            (json.dumps({"termination": {"status": "failed", "reason": "process group still alive"}}),),
        )
        self.assertNotEqual(self.detail()["reason_code"], "analysis_delivered")

    def test_cancel_ends_tracking_and_retains_published_analysis(self):
        detail = self.detail()
        self.assertEqual(detail["reason_code"], "analysis_delivered")
        result = commands.execute_command(
            self.db_path,
            self.item["work_item_id"],
            context=self.context,
            command="cancel",
            expected_version=detail["version"],
            idempotency_key="finish-analysis",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.detail()["column"], "history")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT publish_status FROM github_actions").fetchone()[0], "published")
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)


class BoardRoundCauseTests(BoardReadModelFixture):
    def test_pre_pr_round_error_precedes_process_observation(self):
        item = self._create("Failed candidate", start=True)
        error = "Candidate snapshot changed during final verification; recapture the reviewed head."
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            task = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='failed',expected_output='new_pr'")
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,failure_json) VALUES ('attempt',?,1,'failed',?)",
                (task, json.dumps({"safe_error": "process_missing_or_zombie"})),
            )
            conn.execute(
                "INSERT INTO pre_pr_review_sessions(round_id,task_id,status,request_json,error,created_at,updated_at) "
                "VALUES ('round',?,'attention','{}',?,'2026-11-24','2026-11-24')",
                (task, error),
            )
        detail = board.get_work_item_detail(self.db_path, item["work_item_id"])
        cause = detail["status_summary"]["cause"]
        self.assertEqual(cause["text"], error)
        self.assertEqual(cause["source"], "pre_pr_review")
        self.assertEqual(cause["round_id"], "round")
        self.assertEqual(cause["process_observation"], "process_missing_or_zombie")
        self.assertEqual(detail["execution"]["failure"], "process_missing_or_zombie")
        self.assertTrue(detail["status_summary"]["needs_action"])
        overview = board.list_board(self.db_path)
        card = next(card for card in overview["items"] if card["work_item_id"] == item["work_item_id"])
        self.assertEqual(card["status_summary"]["cause"], cause)
        self.assertEqual(card["reason_summary"], error)

    def test_failed_run_error_survives_an_empty_round_error(self):
        item = self._create("Failed synthesis", start=True)
        error = "Synthesis rejected inconsistent reviewer evidence."
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            task = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle='failed',expected_output='new_pr'")
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,failure_json) VALUES ('attempt',?,1,'failed',?)",
                (task, json.dumps({"safe_error": "process_missing_or_zombie"})),
            )
            conn.execute(
                "INSERT INTO pre_pr_review_sessions(round_id,task_id,status,request_json,created_at,updated_at) "
                "VALUES ('round',?,'attention','{}','2026-11-24','2026-11-24')",
                (task,),
            )
            conn.execute(
                "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,"
                "output_dir,prompt_sha256,error,created_at,updated_at) VALUES ('run','round','synthesis','reviewer',1,"
                "'attempt','failed','/unused','sha',?,'2026-11-24','2026-11-24')",
                (error,),
            )
        detail = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(detail["status_summary"]["cause"]["text"], error)
        self.assertEqual(detail["status_summary"]["cause"]["stage"], "synthesis")
        self.change_for_retry()
        retried = board.get_work_item_detail(self.db_path, item["work_item_id"])
        self.assertEqual(retried["status_summary"]["cause"]["source"], "attempt")
        self.assertEqual(retried["status_summary"]["cause"]["text"], "process_missing_or_zombie")

    def change_for_retry(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,"
                "output_dir,prompt_sha256,created_at,updated_at) VALUES ('retry','round','synthesis','reviewer',2,"
                "'attempt','completed','/retry','sha','2026-11-24','2026-11-24')"
            )

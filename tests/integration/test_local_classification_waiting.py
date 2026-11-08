"""Ambiguous local classification keeps a question that the operator can answer."""

from contextlib import closing
import json
import sqlite3

from tests.support.board_control_integration import BoardControlIntegrationFixture
from robert_agent.delivery.application import submission
from robert_agent.foundation import values
from robert_agent.readmodels import board
from robert_agent.work.application import commands


class LocalClassificationWaitingTests(BoardControlIntegrationFixture):
    def test_github_followup_of_local_item_keeps_public_question_flow(self):
        created = commands.create_work_item(
            self.db_path,
            context=self.context,
            repo_id=self.repo_id,
            title="Initial Web work",
            description="Fix the setting.",
            priority="P2",
            routing_mode="auto",
            requested_worker=None,
            start=True,
            idempotency_key="initial-local-work",
        )
        self._run_local_cycle()
        now = values.utc_now()
        self._finish_task(created["task_id"], now)
        origin = created["item"]["workstream_id"]
        pr_stream = "github:example/repo!81"
        source = values.github_source_id(pr_stream)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE workstreams SET lifecycle='completed', active_task_id=NULL")
            conn.execute(
                """INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,title,state)
                   VALUES (?, ?, ?, 'pull_request', 81, 'https://github.com/example/repo/pull/81',
                           'Repository setting', 'open')""",
                (source, self.repo_id, pr_stream),
            )
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,
                                            lifecycle,created_at,updated_at)
                   VALUES (?, ?, ?, ?, 'completed', ?, ?)""",
                (pr_stream, self.repo_id, source, origin, now, now),
            )
        self.empty_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "ambiguous-pr",
                            "number": 81,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "owner",
                            "author_association": "OWNER",
                            "authorization_lookup_complete": True,
                            "body": "@robot what should we do here?",
                            "intent": "unclear",
                            "url": "https://github.com/example/repo/pull/81",
                            "state": "open",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/previous-work",
                            "origin_workstream_id": origin,
                            "pr_author_login": "robot",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            task_id, attempt_id = conn.execute(
                "SELECT t.task_id, a.attempt_id FROM tasks t JOIN attempts a USING(task_id) WHERE t.workstream_id = ?",
                (pr_stream,),
            ).fetchone()
        receipt = submission.record_result(
            self.db_path,
            submission.build_result(
                task_id=task_id,
                attempt_id=attempt_id,
                output_type="classification_result",
                consumed_event_fingerprints=["comment:ambiguous-pr"],
                verification=[],
                handoff="No route selected.",
            ),
        )
        self.assertTrue(receipt["ok"], receipt)
        self.empty_fixture.write_text('{"events": []}', encoding="utf-8")
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT workstream_id, route_id FROM tasks WHERE parent_task_id = ?", (task_id,)
                ).fetchone(),
                (pr_stream, "waiting-for-user"),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM work_item_events WHERE event_type='operator_question'").fetchone()[
                    0
                ],
                0,
            )

    def test_missing_route_records_one_question_and_accepts_reply(self):
        created = commands.create_work_item(
            self.db_path,
            context=self.context,
            repo_id=self.repo_id,
            title="Investigate behavior",
            description="Please take a look.",
            priority="P2",
            routing_mode="auto",
            requested_worker=None,
            start=True,
            idempotency_key="ambiguous-local-request",
        )
        self._run_local_cycle()
        item_id = created["item"]["work_item_id"]
        with closing(sqlite3.connect(self.db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
            event_ids = [row[0] for row in conn.execute("SELECT event_id FROM work_item_events")]
        receipt = submission.record_result(
            self.db_path,
            submission.build_result(
                task_id=task_id,
                attempt_id=attempt_id,
                output_type="classification_result",
                consumed_event_fingerprints=[],
                consumed_work_item_event_ids=event_ids,
                verification=[],
                handoff="The intended outcome is unclear.",
            ),
        )
        self.assertTrue(receipt["ok"], receipt)
        self._run_local_cycle()
        self._run_local_cycle()
        detail = board.get_work_item_detail(self.db_path, item_id)
        self.assertEqual(detail["column"], "waiting")
        questions = [event for event in detail["events"] if event["event_type"] == "operator_question"]
        self.assertEqual(len(questions), 1)
        self.assertTrue(questions[0]["body"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "waiting_for_user")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
        reply = commands.execute_command(
            self.db_path,
            item_id,
            context=self.context,
            command="reply",
            expected_version=detail["version"],
            idempotency_key="clarify-local-outcome",
            body="Please analyze the current behavior.",
        )
        self.assertTrue(reply["ok"], reply)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT parent_task_id FROM tasks WHERE task_id = ?", (reply["task_id"],)).fetchone()[0],
                task_id,
            )

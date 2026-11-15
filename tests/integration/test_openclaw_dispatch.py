from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

from tests.support.dispatch_requests import DispatchFixture
from robert_agent.work.application import dispatch_requests, admission
from robert_agent.delivery.application import submission, publish, notifications
from robert_agent.persistence import database


class OpenClawDispatchTests(DispatchFixture):
    def classify(self, request_id, route="comment-analysis"):
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        result = submission.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": [f"operator-dispatch:{request_id}"],
                    "verification": [],
                    "handoff": "Route the request",
                    "recommended_route": route,
                    "branch_slug": "dispatch-change",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT t.task_id, a.attempt_id FROM tasks t JOIN attempts a ON a.task_id=t.task_id WHERE parent_task_id=?",
                (task_id,),
            ).fetchone()

    def test_confirmed_issue_starts_one_classifier_without_remote_scan(self):
        request_id = self.create_issue()
        self._run_local_cycle()
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            tasks = conn.execute("SELECT task_id, route_id FROM tasks").fetchall()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0][1], "classification-result")
        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(
                admission.ordinary_dispatch_allowed(conn, tasks[0][0], self.config["repos"][0], self.config)
            )
            revoked = {**self.config, "messaging": {**self.config["messaging"], "dispatch_enabled": False}}
            self.assertFalse(admission.ordinary_dispatch_allowed(conn, tasks[0][0], self.config["repos"][0], revoked))
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")

    def test_classifier_handoff_preserves_operator_authority_and_issue(self):
        request_id = self.create_issue()
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        result = submission.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": [f"operator-dispatch:{request_id}"],
                    "verification": [],
                    "handoff": "Investigate startup",
                    "recommended_route": "comment-analysis",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            child = conn.execute(
                "SELECT task_id, route_id, workstream_id FROM tasks WHERE parent_task_id=?", (task_id,)
            ).fetchone()
            self.assertIsNotNone(child)
            self.assertEqual(child[1:], ("comment-analysis", "github:example/repo#1"))
        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(admission.ordinary_dispatch_allowed(conn, child[0], self.config["repos"][0], self.config))

    def test_default_classification_preserves_unconsumed_dispatch_request(self):
        self.create_issue()
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        self.empty_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "dispatch-context",
                            "number": 1,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "owner",
                            "body": "@robot analyze the startup logs too",
                            "url": "https://github.com/example/repo/issues/1",
                            "state": "open",
                        }
                    ]
                }
            )
        )
        self._run_local_cycle()
        # A continuation can retain the original dispatch as unconsumed context.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE task_events SET relationship='context' WHERE task_id=?", (task_id,))
            conn.execute(
                "UPDATE task_events SET relationship='trigger' WHERE task_id=? AND event_id=?",
                (task_id, "event:comment:dispatch-context"),
            )
        result = submission.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["comment:dispatch-context"],
                    "verification": [],
                    "handoff": "The original request still needs classification.",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            child = conn.execute(
                "SELECT task_id, route_id, workstream_id FROM tasks WHERE parent_task_id=?", (task_id,)
            ).fetchone()
            self.assertIsNotNone(child)
            self.assertEqual(child[1:], ("classification-result", "github:example/repo#1"))
        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(admission.ordinary_dispatch_allowed(conn, child[0], self.config["repos"][0], self.config))

    def _analysis_submission(self, request_id, task_id, attempt_id, *, fingerprints=None):
        return {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "output_type": "comment_analysis",
            "submission": {
                "planned_github_actions": [
                    {
                        "type": "comment",
                        "target_url": "https://github.com/example/repo/issues/1",
                        "body": "启动慢来自重复读取配置。",
                    }
                ],
                "consumed_event_fingerprints": fingerprints
                if fingerprints is not None
                else [f"operator-dispatch:{request_id}"],
                "verification": [],
                "handoff": "已定位启动延迟原因",
                "used_skills": [],
            },
        }

    def _ingress(self, request_id):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT state,result_id FROM request_events WHERE request_id=?",
                (request_id,),
            ).fetchall()

    def complete_analysis(self):
        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        payload = self._analysis_submission(request_id, task_id, attempt_id)
        result = submission.record_result(self.db_path, payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("consumed", result["result_id"])])
        self.assertTrue(submission.record_result(self.db_path, payload)["ok"])
        self.assertEqual(self._ingress(request_id), [("consumed", result["result_id"])])
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")

        def runner(argv, **kwargs):
            value = (
                []
                if "--paginate" in argv
                else {"id": 987, "html_url": "https://github.com/example/repo/issues/1#issuecomment-987"}
            )
            return SimpleNamespace(stdout=json.dumps(value), stderr="", returncode=0)

        outcome = publish.publish_ready_actions(self.db_path, run_command=runner, config=self.config)
        self.assertTrue(outcome["ok"], outcome)
        self._run_local_cycle()
        self._run_local_cycle()
        return request_id

    def test_analysis_publishes_issue_result_and_reports_completion_once(self):
        request_id = self.complete_analysis()
        status = self.command(request_id, "status")
        self.assertEqual(status["status"], "delivered", status)
        self.assertIsNone(status["result"]["pr_url"])
        sent = []

        def send(argv, **kwargs):
            sent.append(argv[argv.index("--message") + 1])
            return SimpleNamespace(stdout='{"ok":true}', returncode=0)

        notifications.deliver_pending(self.db_path, config=self.config, runner=send)
        notifications.deliver_pending(self.db_path, config=self.config, runner=send)
        self.assertEqual(sum("请求已完成，结果已发布" in item for item in sent), 1)
        self.assertTrue(all("dispatch_completed" not in item for item in sent))
        self.assertTrue(any("issuecomment-987" in item for item in sent))

    def test_implementation_is_selected_without_user_specifying_pr(self):
        request_id = self.create_issue()
        task_id, _ = self.classify(request_id, "new-pr")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT route_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0], "new-pr"
            )
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")

    def test_scanner_replay_does_not_duplicate_initial_dispatch(self):
        self.create_issue()
        self.empty_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assigned-1",
                            "number": 1,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "owner",
                            "assigned_to": "robot",
                            "assignment_actor_login": "owner",
                            "url": "https://github.com/example/repo/issues/1",
                            "state": "open",
                            "body": "调研启动慢的原因",
                            "title": "调研启动慢的原因",
                            "source_author_login": "robot",
                        }
                    ]
                }
            )
        )
        self._run_local_cycle()
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_question_reply_resumes_classification_without_completing_dispatch(self):
        from datetime import datetime, timezone
        from robert_agent.adapters.github.client import command_key
        from robert_agent.work.application import messaging

        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id, "waiting-for-user")
        result = submission.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "waiting_for_user",
                "submission": {
                    "planned_github_actions": [
                        {
                            "type": "comment",
                            "target_url": "https://github.com/example/repo/issues/1",
                            "body": "调查哪个启动阶段？",
                        }
                    ],
                    "consumed_event_fingerprints": [f"operator-dispatch:{request_id}"],
                    "verification": [],
                    "handoff": "等待确认范围",
                    "used_skills": [],
                    "operator_question": {"kind": "clarification", "summary": "调查哪个启动阶段？", "choices": []},
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()

        def runner(argv, **kwargs):
            return SimpleNamespace(
                stdout=json.dumps(
                    []
                    if "--paginate" in argv
                    else {"id": 987, "html_url": "https://github.com/example/repo/issues/1#issuecomment-987"}
                ),
                stderr="",
                returncode=0,
            )

        self.assertTrue(publish.publish_ready_actions(self.db_path, run_command=runner, config=self.config)["ok"])
        self._run_local_cycle()
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        notice = next(
            item for item in messaging.inbox(self.config, self.identity)["messages"] if "reply" in item["actions"]
        )
        command = {
            **self.identity,
            "notification_id": notice["notification_id"],
            "version": notice["version"],
            "action": "reply",
            "body": "仅调研配置加载阶段，不修改代码。",
        }
        reply = messaging.respond(self.config, command)
        self.assertEqual(reply, messaging.respond(self.config, command))
        self._run_local_cycle()  # No source observation yet: the child must wait.
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM attempts WHERE task_id=?", (reply["task_id"],)).fetchone()[0], 0
            )
            conn.execute(
                "INSERT OR REPLACE INTO github_scan_reads VALUES (?, ?, ?, ?)",
                (
                    self.repo_id,
                    command_key(["gh", "api", "repos/example/repo/issues/1"]),
                    json.dumps({"body": "调研启动慢的原因", "state": "open", "title": "调研启动慢的原因"}),
                    datetime.now(timezone.utc).timestamp() + 1,
                ),
            )
        self._run_local_cycle()
        status = self.command(request_id, "status")
        self.assertEqual(status["task"]["task_id"], reply["task_id"])
        self.assertEqual(status["task"]["route_id"], "classification-result")
        from robert_agent.context import attempt

        with closing(database.connect(self.db_path)) as conn:
            prompt = attempt.prompt_path_for_task(conn, self.data_dir, reply["task_id"])
        # The registered context carries the answer through the existing lifecycle.
        from pathlib import Path

        self.assertIn("仅调研配置加载阶段", Path(prompt).read_text())

        with closing(sqlite3.connect(self.db_path)) as conn:
            [[attempt_id]] = conn.execute("SELECT attempt_id FROM attempts WHERE task_id=?", (reply["task_id"],))
        routed = submission.record_result(
            self.db_path,
            {
                "task_id": reply["task_id"],
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": [f"operator-dispatch:{request_id}"],
                    "verification": [],
                    "handoff": "Analyze the clarified startup phase",
                    "recommended_route": "comment-analysis",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(routed["ok"], routed)
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        with closing(sqlite3.connect(self.db_path)) as conn:
            task_id, attempt_id = conn.execute(
                "SELECT t.task_id, a.attempt_id FROM tasks t JOIN attempts a ON a.task_id=t.task_id WHERE parent_task_id=?",
                (reply["task_id"],),
            ).fetchone()
        final = submission.record_result(self.db_path, self._analysis_submission(request_id, task_id, attempt_id))
        self.assertTrue(final["ok"], final)
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("consumed", final["result_id"])])
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")

    def create_issue_scanner_first(self):
        request_id = self.draft()["request_id"]
        self.command(request_id)

        def runner(argv, **kwargs):
            if argv[-1] == "user":
                return SimpleNamespace(stdout='{"login":"robot"}', returncode=0)
            self.empty_fixture.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "id": "scanner-first",
                                "number": 1,
                                "source_type": "issue",
                                "event_type": "assigned",
                                "actor_login": "owner",
                                "assigned_to": "robot",
                                "assignment_actor_login": "owner",
                                "url": "https://github.com/example/repo/issues/1",
                                "state": "open",
                                "body": "调研启动慢的原因",
                            }
                        ]
                    }
                )
            )
            self._run_local_cycle()
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "id": 12,
                        "number": 1,
                        "html_url": "https://github.com/example/repo/issues/1",
                        "user": {"login": "robot"},
                        "state": "open",
                        "body": json.loads(kwargs["input"])["body"],
                    }
                ),
                returncode=0,
            )

        dispatch_requests.advance(self.config, runner=runner)
        return request_id

    def test_scanner_can_admit_before_creation_receipt_without_crashing_dispatch(self):
        request_id = self.create_issue_scanner_first()
        self._run_local_cycle()
        self.assertEqual(self.command(request_id, "status")["status"], "issue_created")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
        from robert_agent.work import dispatch_requests as facts

        with closing(database.connect(self.db_path)) as conn:
            self.assertEqual(facts.pending(conn, self.config, repo_ids=[self.repo_id]), [])

    def test_waiting_dispatch_preserves_a_fresher_closed_issue_observation(self):
        from robert_agent.work.application import source_lifecycle
        from robert_agent.foundation import values

        request_id = self.create_issue_scanner_first()
        self.empty_fixture.write_text('{"events": []}')

        def runner(*args, **kwargs):
            return SimpleNamespace(stdout='{"state":"closed"}', returncode=0)

        with closing(database.connect(self.db_path)) as conn, conn:
            source_lifecycle.reconcile_remote_source_states(
                conn, self.repo_id, self.config["repos"][0], runner, values.utc_now()
            )
        self._run_local_cycle()
        self._run_local_cycle()
        self.assertEqual(self.command(request_id, "status")["status"], "attention")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT state FROM sources").fetchone()[0], "closed")
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_revoked_dispatch_config_retires_publication_without_write(self):
        self._assert_denied_publication(False)

    def test_missing_dispatch_config_retires_publication_without_write(self):
        self._assert_denied_publication(True)

    def _assert_denied_publication(self, absent):
        from robert_agent.delivery import queries

        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        result = submission.record_result(
            self.db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "submission": {
                    "planned_github_actions": [
                        {"type": "comment", "target_url": "https://github.com/example/repo/issues/1", "body": "Result"}
                    ],
                    "consumed_event_fingerprints": [f"operator-dispatch:{request_id}"],
                    "verification": [],
                    "handoff": "Result",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        revoked = {**self.config, "messaging": {**self.config["messaging"], "dispatch_enabled": False}}
        raw = json.loads(self.config_path.read_text())
        raw["messaging"]["dispatch_enabled"] = False
        self.config_path.write_text(json.dumps(raw))
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(stdout="[]", stderr="", returncode=0)

        outcome = publish.publish_ready_actions(self.db_path, run_command=runner, config=None if absent else revoked)
        self.assertFalse(outcome["ok"], outcome)
        self.assertFalse(any("-f" in argv or "POST" in argv for argv in calls), calls)
        with closing(database.connect(self.db_path)) as conn:
            self.assertEqual(queries.ready_actions(conn, 5, repo_id=self.repo_id), [])

    def test_fresh_github_request_does_not_inherit_dispatch_authority_or_status(self):
        from robert_agent.persistence.queries import dispatch_requests as requests

        request_id = self.complete_analysis()
        prior_status = self.command(request_id, "status")
        self.empty_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "followup-100",
                            "number": 1,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "owner",
                            "body": "@robot research an independent topic",
                            "state": "open",
                            "url": "https://github.com/example/repo/issues/1",
                            "source_author_login": "robot",
                        }
                    ]
                }
            )
        )
        self._run_local_cycle()
        with closing(database.connect(self.db_path)) as conn:
            from robert_agent.persistence.queries import tasks

            # The new task has its own lineage despite sharing Issue and history.
            rows = tasks.find_task_id_by_workstream_id(conn, "github:example/repo#1")
            independent = [row[0] for row in rows if requests.for_task(conn, row[0]) is None]
            self.assertEqual(len(independent), 1)
            revoked = {**self.config, "messaging": {**self.config["messaging"], "dispatch_enabled": False}}
            self.assertTrue(admission.ordinary_dispatch_allowed(conn, independent[0], self.config["repos"][0], revoked))
        self.assertEqual(self.command(request_id, "status"), prior_status)

    def test_dispatch_worker_requires_current_config_even_for_direct_launcher(self):
        from unittest.mock import patch
        from robert_agent.execution.application import dispatch as launcher

        self.create_issue()
        self._run_local_cycle()
        queue = []
        with closing(database.connect(self.db_path)) as conn:
            launcher.queue_prepared_attempts(conn, self.repo_id, self.config["repos"][0], queue, 2)
        self.assertEqual(len(queue), 1)
        with patch.object(launcher.dispatch, "dispatch_worker") as launch:
            launcher.dispatch_queued(
                self.db_path, [self.repo_id], queue, False, 2, workers=self.config["workers"], config_result=None
            )
        launch.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed")

    def test_confirmed_dispatch_durably_bridges_before_worker_capacity(self):
        request_id = self.create_issue()
        with closing(sqlite3.connect(self.db_path)) as conn:
            [[provider, kind, auth, fingerprint, state, result_id]] = conn.execute(
                """SELECT i.provider, s.source_type, i.authorization_json, e.event_fingerprint, l.state, l.result_id
                   FROM ingress_requests i JOIN sources s ON s.source_id=i.source_id
                   JOIN request_events l ON l.request_id=i.request_id
                   JOIN source_events e ON e.event_id=l.event_id WHERE i.request_id=?""",
                (request_id,),
            ).fetchall()
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone(), (0,))
        self.assertEqual(
            (provider, kind, fingerprint, state, result_id),
            ("github", "issue", f"operator-dispatch:{request_id}", "pending", None),
        )
        self.assertEqual(json.loads(auth)["identity"], self.identity)
        self.assertEqual(self.command(request_id, "status")["status"], "issue_created")

    def test_unknown_issue_restart_creates_no_bridge_until_exact_readback(self):
        import subprocess

        request_id = self.draft()["request_id"]
        self.command(request_id)
        posts = []
        proven = {}

        def runner(argv, **kwargs):
            if argv[-1] == "user":
                return SimpleNamespace(stdout='{"login":"robot"}', returncode=0)
            if "POST" in argv:
                posts.append(argv)
                proven.update(
                    id=13,
                    number=2,
                    html_url="https://github.com/example/repo/issues/2",
                    user={"login": "robot"},
                    state="open",
                    body=json.loads(kwargs["input"])["body"],
                )
                raise subprocess.TimeoutExpired(argv, 30)
            return SimpleNamespace(stdout=json.dumps([[proven]]), returncode=0)

        dispatch_requests.advance(self.config, runner=runner)
        self.assertEqual(self._ingress(request_id), [])
        self.assertEqual(self.command(request_id, "status")["status"], "attention")
        self.command(request_id, "retry")
        dispatch_requests.advance(self.config, runner=runner)
        dispatch_requests.advance(self.config, runner=runner)
        self.assertEqual(len(posts), 1)
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone(), (0,))

    def test_final_result_missing_original_event_cannot_commit_accepted_actions(self):
        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        payload = self._analysis_submission(request_id, task_id, attempt_id, fingerprints=["comment:unrelated"])
        result = submission.record_result(self.db_path, payload)
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT json_extract(metadata_json,'$.audit.status') FROM worker_results WHERE result_id=?",
                    (result["result_id"],),
                ).fetchone(),
                ("policy_violation",),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT audit_status FROM github_actions WHERE result_id=?", (result["result_id"],)
                ).fetchall(),
                [("policy_violation",)],
            )

    def test_bridge_recovers_historical_accepted_result_without_repeating_worker_or_issue(self):
        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        result = submission.record_result(self.db_path, self._analysis_submission(request_id, task_id, attempt_id))
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("consumed", result["result_id"])])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM request_events WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM ingress_requests WHERE request_id=?", (request_id,))
        self._run_local_cycle()
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("consumed", result["result_id"])])
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone(), (2,))
            self.assertEqual(conn.execute("SELECT count(*) FROM github_actions").fetchone(), (1,))

    def test_creation_confirmation_keeps_exact_receipt_during_sender_revocation(self):
        request_id = self.draft()["request_id"]
        self.command(request_id)

        def runner(argv, **kwargs):
            if argv[-1] == "user":
                return SimpleNamespace(stdout='{"login":"robot"}', returncode=0)
            self.config["messaging"]["allowed_senders"] = ["43"]
            raw = json.loads(self.config_path.read_text())
            raw["messaging"]["allowed_senders"] = ["43"]
            self.config_path.write_text(json.dumps(raw))
            return SimpleNamespace(
                stdout=json.dumps(
                    {
                        "id": 12,
                        "number": 1,
                        "html_url": "https://github.com/example/repo/issues/1",
                        "user": {"login": "robot"},
                        "state": "open",
                        "body": json.loads(kwargs["input"])["body"],
                    }
                ),
                returncode=0,
            )

        dispatch_requests.advance(self.config, runner=runner)
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM dispatch_requests").fetchone(), ("issue_created",))
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone(), (0,))

    def test_audit_write_failure_rolls_back_ingress_and_acceptance_together(self):
        from unittest.mock import patch

        from robert_agent.delivery import results
        from robert_agent.delivery.application import result_audit
        from robert_agent.foundation import values

        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        saved = submission.record_result(self.db_path, self._analysis_submission(request_id, task_id, attempt_id))
        self.assertTrue(saved["ok"], saved)
        actual = results.set_github_actions_status

        def interrupted(conn, result_id, status):
            actual(conn, result_id, status)
            raise RuntimeError("interrupted acceptance")

        with closing(database.connect(self.db_path)) as conn, conn:
            with patch.object(results, "set_github_actions_status", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "interrupted acceptance"):
                    result_audit.audit_completed_results(
                        conn,
                        self.repo_id,
                        self.data_dir,
                        self.db_path,
                        self.config["repos"][0],
                        "local-test-run",
                        values.utc_now(),
                        True,
                        [],
                        {"remaining": 0},
                        config_result=self.config,
                        task_id=task_id,
                    )
            conn.commit()
        self.assertEqual(self._ingress(request_id), [("pending", None)])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT json_extract(metadata_json,'$.audit.status') FROM worker_results WHERE result_id=?",
                    (saved["result_id"],),
                ).fetchone()[0]
            )
            self.assertEqual(
                conn.execute(
                    "SELECT audit_status FROM github_actions WHERE result_id=?", (saved["result_id"],)
                ).fetchone(),
                ("pending",),
            )
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("consumed", saved["result_id"])])

    def test_final_implementation_consumes_then_requires_pr_and_original_issue_receipts(self):
        from pathlib import Path
        import subprocess

        from tests.support import init_test_checkout
        from tests.support.repository_cycle import _verification_evidence

        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id, "new-pr")
        with closing(sqlite3.connect(self.db_path)) as conn:
            worktree, branch = conn.execute(
                "SELECT worktree_path,branch_name FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
        root = Path(worktree)
        root.mkdir(parents=True, exist_ok=True)
        init_test_checkout(root, branch)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/robot/repo.git"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        payload = self._analysis_submission(request_id, task_id, attempt_id)
        payload["output_type"] = "new_pr"
        payload["submission"]["planned_github_actions"][:0] = [
            {"type": "push_existing_pr"},
            {
                "type": "open_pr",
                "title": "Fix startup",
                "body": "Read initialization settings once.",
                "issue_resolution": "complete",
            },
        ]
        payload["submission"].update(verification=[_verification_evidence()], used_skills=["fast-small-pr"])
        saved = submission.record_result(self.db_path, payload)
        self.assertTrue(saved["ok"], saved)
        self._run_local_cycle()
        self.assertEqual(self._ingress(request_id), [("consumed", saved["result_id"])])
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT DISTINCT audit_status FROM github_actions").fetchall(), [("accepted",)]
            )

        issue_url = "https://github.com/example/repo/issues/1"
        pr_url = "https://github.com/example/repo/pull/9"
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["git", "push"]:
                stdout = ""
            elif argv[0] == "git":
                return subprocess.run(argv, **kwargs)
            elif argv[:3] == ["gh", "pr", "create"]:
                stdout = pr_url
            elif "-f" in argv:
                self.assertEqual(argv[2], "repos/example/repo/issues/1/comments")
                stdout = json.dumps({"id": 987, "html_url": f"{issue_url}#issuecomment-987"})
            else:
                stdout = "[]"
            return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        first = publish.publish_ready_actions(self.db_path, config=self.config, run_command=runner, limit=2)
        self.assertEqual(first["published_count"], 2, first)
        self._run_local_cycle()
        self.assertEqual(self.command(request_id, "status")["status"], "admitted")
        second = publish.publish_ready_actions(self.db_path, config=self.config, run_command=runner)
        self.assertEqual(second["published_count"], 1, second)
        self._run_local_cycle()
        status = self.command(request_id, "status")
        self.assertEqual(status["status"], "delivered", status)
        self.assertEqual(status["result"]["pr_url"], pr_url)
        self.assertEqual(status["result"]["result_url"], f"{issue_url}#issuecomment-987")
        self.assertEqual(sum(call[:3] == ["gh", "pr", "create"] for call in calls), 1)

    def test_historical_bridge_pages_past_blocked_rows_and_survives_restart(self):
        from robert_agent.work.application import dispatch_ingress
        from robert_agent.foundation import values

        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        result = submission.record_result(self.db_path, self._analysis_submission(request_id, task_id, attempt_id))
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM request_events WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM ingress_requests WHERE request_id=?", (request_id,))
            for index in range(51):
                conn.execute(
                    "INSERT INTO dispatch_requests(request_id,repo_id,identity_json,title,body,status,issue_json,"
                    "workstream_id,created_at,updated_at) SELECT ?,repo_id,identity_json,title,body,'admitted','{}',"
                    "?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00' FROM dispatch_requests WHERE request_id=?",
                    (f"blocked-{index:03}", f"blocked-stream-{index:03}", request_id),
                )
            conn.execute(
                "UPDATE repos SET metadata_json=? WHERE repo_id=?", (json.dumps({"preserve": "value"}), self.repo_id)
            )
        now = values.utc_now()
        with closing(database.connect(self.db_path)) as conn, conn:
            dispatch_ingress.recover_confirmed(conn, self.config, self.repo_id, now)
        self.assertEqual(self._ingress(request_id), [])
        with closing(database.connect(self.db_path)) as conn, conn:
            dispatch_ingress.recover_confirmed(conn, self.config, self.repo_id, now)
        self.assertEqual(self._ingress(request_id), [("consumed", result["result_id"])])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(
                json.loads(
                    conn.execute("SELECT metadata_json FROM repos WHERE repo_id=?", (self.repo_id,)).fetchone()[0]
                )["preserve"],
                "value",
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone(), (2,))
            self.assertEqual(conn.execute("SELECT count(*) FROM ingress_requests").fetchone(), (1,))

    def test_dormant_historical_bridge_is_local_work_without_wakeup_or_capacity(self):
        from robert_agent.runtime import loop
        from robert_agent.work.application import dispatch_ingress
        from robert_agent.foundation import values

        request_id = self.create_issue()
        task_id, attempt_id = self.classify(request_id)
        result = submission.record_result(self.db_path, self._analysis_submission(request_id, task_id, attempt_id))
        self.assertTrue(result["ok"], result)
        self._run_local_cycle()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM request_events WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM ingress_requests WHERE request_id=?", (request_id,))
            conn.execute("DELETE FROM wakeups")
        config = {**self.config, "max_concurrency": 0}
        with closing(database.connect(self.db_path)) as conn, conn:
            self.assertTrue(
                loop.has_runnable_local_work(
                    conn, repo_ids=[self.repo_id], config_result=config, include_publication=False
                )
            )
            dispatch_ingress.recover_confirmed(conn, config, self.repo_id, values.utc_now())
        self.assertEqual(self._ingress(request_id), [("consumed", result["result_id"])])

    def test_blocked_historical_recovery_is_paced_and_scope_checked(self):
        from datetime import timedelta

        from robert_agent.foundation import values
        from robert_agent.runtime import loop
        from robert_agent.work.application import dispatch_ingress

        request_id = self.draft()["request_id"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE dispatch_requests SET status='admitted' WHERE request_id=?", (request_id,))
        now = values.utc_now()
        later = (values.parse_time(now) + timedelta(seconds=121)).isoformat()
        with closing(database.connect(self.db_path)) as conn, conn:
            options = {"repo_ids": [self.repo_id], "config_result": self.config, "include_publication": False}
            self.assertTrue(loop.has_runnable_local_work(conn, now=now, **options))
            dispatch_ingress.recover_confirmed(conn, self.config, self.repo_id, now)
            self.assertFalse(loop.has_runnable_local_work(conn, now=now, **options))
        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(loop.has_runnable_local_work(conn, now=later, **options))
            disabled = {**self.config, "messaging": {**self.config["messaging"], "dispatch_enabled": False}}
            self.assertFalse(loop.has_runnable_local_work(conn, now=later, **{**options, "config_result": disabled}))
            self.assertFalse(
                loop.has_runnable_local_work(conn, now=later, **{**options, "repo_ids": ["repo:elsewhere/repo"]})
            )
        self.assertEqual(self._ingress(request_id), [])

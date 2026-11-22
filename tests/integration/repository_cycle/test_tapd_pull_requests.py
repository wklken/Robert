"""TAPD development requests open or update one derived PR and answer on the ticket."""

from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest import mock

from robert_agent.adapters.github import client as github_client
from robert_agent.adapters.github import sources as github_sources
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.configuration import validation
from robert_agent.delivery.application import scope
from robert_agent.delivery.application import submission, tapd_comments, tapd_notifications
from robert_agent.readmodels import board
from robert_agent.runtime import cycle as run_once
from robert_agent.runtime import loop
from robert_agent.work.application import commands, remediation_requests, source_lifecycle, synchronization, tapd_scan
from robert_agent.workflows import pr_health, pr_remediation
from tests.support.work_items import example_choices
from tests.support.repository_cycle import _verification_evidence
from tests.support.tapd import STORY, comment
from tests.support.tapd_cycle import TapdCycleCase

PR_URL = "https://github.com/example/backend/pull/10"
PR_WORKSTREAM = "github:example/backend!10"
TICKET_WORKSTREAM = f"tapd:tapd.example/100/story/{STORY}:example/backend"
MERGED = {
    "state": "closed",
    "merged": True,
    "merged_at": "2026-11-20T04:00:00Z",
    "closed_at": "2026-11-20T04:00:00Z",
    "html_url": PR_URL,
}


class TapdPullRequestCase(TapdCycleCase):
    def request_fix(self, comment_id="2", *, pr_body="Retry the session refresh once.", commit_message="Fix timeout"):
        self.scan(comment(comment_id, "@robert-wklken please fix this bug"))
        self.cycle()
        task_id, attempt_id, worktree, branch = self.query(
            "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts ORDER BY rowid DESC LIMIT 1"
        )[0]
        self._materialize_worktree(worktree, branch)
        for args in (
            ["update-ref", "refs/remotes/upstream/master", "HEAD"],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                commit_message,
            ],
        ):
            subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True)
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": [
                        {"type": "push_existing_pr"},
                        {"type": "open_pr", "title": "Fix login timeout", "body": pr_body},
                        {"type": "comment", "body": "已修复登录超时，单元测试通过。"},
                    ],
                    "consumed_event_fingerprints": [f"tapd-comment:tapd.example/100/{comment_id}"],
                    "verification": [_verification_evidence()],
                    "handoff": "opened PR",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        return task_id

    def open_pull_request(self):
        """Stand in for GitHub: the push and the PR are confirmed."""
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published', "
                "target_url = CASE WHEN action_type = 'open_pr' THEN ? ELSE target_url END "
                "WHERE action_type IN ('push_existing_pr', 'open_pr')",
                (PR_URL,),
            )

    def publish(self, remote_state=None):
        with (
            mock.patch.object(tapd_client, "client_for", return_value=self.client),
            mock.patch.object(
                github_sources,
                "read_source_state",
                return_value={"state": "open"} if remote_state is None else remote_state,
            ),
        ):
            outcome = run_once.run_once(self.config_path, dry_run=False, skip_external=True, max_dispatches=0)
        return outcome

    def make_retry_due(self):
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET metadata_json = "
                "json_set(metadata_json, '$.publish.next_retry_at', '2000-01-01T00:00:00+00:00') "
                "WHERE action_type = 'comment'"
            )

    def reconcile(self, remote_state, *, review_runner=None):
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        with (
            closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn,
            mock.patch.object(github_sources, "read_source_state", return_value=remote_state),
        ):
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            for _ in range(2):
                source_lifecycle.reconcile_remote_source_states(
                    conn,
                    repo_id,
                    repo,
                    review_runner,
                    "2026-11-20T04:10:00+00:00",
                    tapd_settings=config["tapd"] if review_runner else None,
                )
            conn.commit()


class TapdNewPullRequestTests(TapdPullRequestCase):
    def test_result_comment_is_deferred_if_github_does_not_confirm_pr_state(self):
        self.request_fix()
        self.open_pull_request()
        with (
            mock.patch.object(tapd_client, "client_for", return_value=self.client),
            mock.patch.object(github_client, "try_read_json", return_value={}),
        ):
            run_once.run_once(self.config_path, dry_run=False, skip_external=True, max_dispatches=0)

        self.assertEqual(self.client.written, [])
        self.assertEqual(
            self.query("SELECT publish_status FROM github_actions WHERE action_type = 'comment'"),
            [("not_published",)],
        )

    def test_result_comment_rechecks_pr_before_retry_without_a_prior_terminal_observation(self):
        self.request_fix()
        self.open_pull_request()
        self.client.reject_writes = 1
        self.publish()
        self.make_retry_due()

        self.publish(remote_state=MERGED)

        self.assertEqual(self.client.written, [])
        self.assertEqual(
            self.query("SELECT publish_status FROM github_actions WHERE action_type = 'comment'"),
            [("skipped",)],
        )
        self.reconcile(MERGED)
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("canceled",)])

    def test_new_pull_request_is_answered_on_the_ticket_with_its_link(self):
        self.request_fix()
        self.open_pull_request()
        self.assertTrue(self.publish()["ok"])
        (body,) = self.client.written
        self.assertIn("已修复登录超时", body)
        self.assertIn(PR_URL, body)
        self.assertEqual(
            self.query("SELECT origin_workstream_id FROM workstreams WHERE workstream_id = ?", (PR_WORKSTREAM,)),
            [(TICKET_WORKSTREAM,)],
        )
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("completed",)])
        ((marker,),) = self.query("SELECT json_extract(metadata_json, '$.body') FROM github_actions "
                                  "WHERE action_type = 'open_pr'")  # fmt: skip
        ((public_ref,),) = self.query(
            "SELECT public_ref FROM workstreams WHERE workstream_id = ?", (TICKET_WORKSTREAM,)
        )
        self.assertIn(f"origin_workstream_id: {public_ref}", marker)
        self.assertNotIn("tapd", marker)

    def test_final_audit_rejects_tapd_identifiers_in_the_normalized_pr(self):
        self.request_fix(pr_body=f"See https://tapd.example/100/prong/stories/view/{STORY}")
        self.cycle()
        self.assertEqual(
            set(self.query("SELECT audit_status, publish_status FROM github_actions")),
            {("policy_violation", "not_published")},
        )

    def test_final_audit_rejects_tapd_identifiers_in_commits_without_echoing_them(self):
        self.request_fix(commit_message=f"Fix timeout for ticket {STORY}")
        self.cycle()
        self.assertEqual(
            set(self.query("SELECT audit_status, publish_status FROM github_actions")),
            {("policy_violation", "not_published")},
        )
        [(audit, violation)] = self.query(
            "SELECT json_extract(metadata_json, '$.audit.safe_error'), "
            "json_extract(metadata_json, '$.audit.violations[0]') FROM worker_results"
        )
        self.assertEqual(violation, "tapd_identifier_in_commit")
        self.assertNotIn(STORY, audit)

    def test_failed_ticket_comment_keeps_the_link_and_retries_the_same_body(self):
        self.request_fix()
        self.open_pull_request()
        self.client.reject_writes = 1
        self.publish()
        self.assertEqual(len(self.client.rejected), 1)
        self.assertIn(PR_URL, self.client.rejected[0])
        self.assertEqual(
            self.query("SELECT origin_workstream_id FROM workstreams WHERE workstream_id = ?", (PR_WORKSTREAM,)),
            [(TICKET_WORKSTREAM,)],
        )
        self.assertNotEqual(self.query("SELECT lifecycle FROM tasks"), [("completed",)])
        self.make_retry_due()
        self.assertTrue(self.publish()["ok"])
        self.assertEqual(self.client.written, self.client.rejected)
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("completed",)])

    def test_pull_request_merged_before_the_ticket_comment_ends_the_round(self):
        self.request_fix()
        self.open_pull_request()
        self.client.reject_writes = 1
        self.publish()
        self.reconcile(MERGED)
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("canceled",)])
        self.assertEqual(
            self.query("SELECT active_task_id FROM workstreams WHERE workstream_id = ?", (TICKET_WORKSTREAM,)),
            [(None,)],
        )
        self.make_retry_due()
        self.scan(comment("3", "@robert-wklken please also fix the logout bug", "2026-11-20 12:00:00"))
        self.cycle()
        self.assertEqual(
            self.query(
                "SELECT t.workstream_id, t.route_id, a.status FROM tasks t JOIN attempts a USING(task_id) "
                "JOIN task_events te USING(task_id) JOIN github_events ge USING(event_id) "
                "WHERE ge.event_fingerprint LIKE '%/3' AND te.relationship = 'trigger'"
            ),
            [(TICKET_WORKSTREAM, "new-pr", "prepared")],
        )
        self.assertEqual(self.query("SELECT status FROM publication_intents"), [("executing",)])
        self.publish()
        self.assertEqual(self.client.written, [])


class TapdDerivedQueueTests(TapdPullRequestCase):
    def start_pr(self):
        self.request_fix()
        self.open_pull_request()
        self.assertTrue(self.publish()["ok"])

    def pr_tasks(self):
        return self.query(
            "SELECT t.route_id, ge.event_fingerprint, te.relationship FROM tasks t JOIN task_events te USING(task_id) "
            "JOIN github_events ge USING(event_id) WHERE t.workstream_id = ? ORDER BY t.created_at, te.relationship",
            (PR_WORKSTREAM,),
        )

    def submit_pr_answer(self, fingerprint, output="comment_analysis", additional_requests=()):
        [(task, attempt)] = self.query(
            "SELECT task_id, attempt_id FROM tasks JOIN attempts USING(task_id) "
            "WHERE workstream_id = ? ORDER BY tasks.rowid DESC LIMIT 1",
            (PR_WORKSTREAM,),
        )
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task,
                "attempt_id": attempt,
                "output_type": output,
                "submission": {
                    "planned_github_actions": [{"type": "comment", "body": "Answer."}],
                    "consumed_event_fingerprints": [fingerprint, *additional_requests],
                    "verification": [],
                    "handoff": "answered",
                    "used_skills": [],
                    **({"review_input_kind": "request"} if output == "review_comment" else {}),
                },
            },
        )
        self.assertTrue(record["ok"], record)
        return task

    def test_ticket_round_left_in_attention_does_not_hold_back_the_pr_queue(self):
        self.request_fix()
        self.open_pull_request()
        self.client.reject_writes = 10
        for _ in range(4):
            self.publish()
            self.make_retry_due()
        self.assertEqual(self.query("SELECT status FROM publication_intents"), [("attention",)])
        self.assertEqual(
            self.query("SELECT t.lifecycle FROM tasks t JOIN workstreams w ON w.active_task_id = t.task_id"),
            [("running",)],
        )
        self.scan(comment("3", "@robert-wklken 请分析退出流程", "2026-11-20 12:00:00"))
        self.cycle()
        self.assertEqual(self.pr_tasks(), [("comment-analysis", "tapd-comment:tapd.example/100/3", "trigger")])
        self.client.reject_writes = 0
        self.submit_pr_answer("tapd-comment:tapd.example/100/3")
        self.publish()
        self.assertEqual(
            self.query("SELECT lifecycle FROM tasks WHERE workstream_id = ?", (PR_WORKSTREAM,)), [("completed",)]
        )

        self.scan(comment("4", "@robert-wklken 请分析登录流程", "2026-11-20 12:05:00"))
        self.cycle()
        self.assertIn(("comment-analysis", "tapd-comment:tapd.example/100/4", "trigger"), self.pr_tasks())
        self.assertIn(("attention",), self.query("SELECT status FROM publication_intents"))

    def test_requests_queued_behind_the_pr_merge_into_one_round(self):
        self.start_pr()
        self.scan(
            comment("3", "@robert-wklken please fix the logout bug", "2026-11-20 12:00:00"),
            comment("4", "@robert-wklken and the session refresh too", "2026-11-20 12:01:00"),
        )
        self.cycle()
        self.cycle()
        self.assertEqual(
            self.pr_tasks(),
            [
                ("update-existing-pr", "tapd-comment:tapd.example/100/4", "context"),
                ("update-existing-pr", "tapd-comment:tapd.example/100/3", "trigger"),
            ],
        )

    def test_reopened_pr_followup_waits_for_an_active_ticket_round(self):
        self.start_pr()
        self.scan(comment("3", "@robert-wklken 请分析", "2026-11-20 12:00:00"))
        self.cycle()
        self.reconcile({**MERGED, "merged": False, "merged_at": None})
        self.scan(comment("4", "@robert-wklken please fix the logout bug", "2026-11-20 12:01:00"))
        self.cycle()
        self.assertEqual(
            self.query(
                "SELECT t.route_id FROM tasks t JOIN workstreams w ON w.active_task_id = t.task_id "
                "WHERE w.workstream_id = ?",
                (TICKET_WORKSTREAM,),
            ),
            [("new-pr",)],
        )
        [(ref,)] = self.query("SELECT public_ref FROM workstreams WHERE workstream_id = ?", (TICKET_WORKSTREAM,))
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "reopened-pr-followup",
                            "event_type": "comment",
                            "source_type": "pull_request",
                            "number": 10,
                            "body": "@robert-bot please analyze the login timeout",
                            "actor_login": "wklken",
                            "state": "open",
                            "url": PR_URL,
                            "pr_author_login": "robert-bot",
                            "metadata": {"dd_workstream": {"origin_workstream_id": ref}},
                        }
                    ]
                }
            )
        )
        self.cycle()
        self.assertEqual(
            self.query("SELECT lifecycle FROM tasks WHERE workstream_id = ?", (PR_WORKSTREAM,)), [("canceled",)]
        )

    def test_tapd_round_starts_with_all_requests_queued_behind_a_github_task(self):
        self.start_pr()
        [(ref,)] = self.query("SELECT public_ref FROM workstreams WHERE workstream_id = ?", (TICKET_WORKSTREAM,))
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "github-followup",
                            "event_type": "comment",
                            "source_type": "pull_request",
                            "number": 10,
                            "body": "@robert-bot please analyze the login timeout",
                            "actor_login": "wklken",
                            "state": "open",
                            "url": PR_URL,
                            "pr_author_login": "robert-bot",
                            "metadata": {"dd_workstream": {"origin_workstream_id": ref}},
                        }
                    ]
                }
            )
        )
        github_run = self.cycle()
        self.scan(
            comment("3", "@robert-wklken 请分析登录流程", "2026-11-20 12:00:00"),
            comment("4", "@robert-wklken 再分析会话续期", "2026-11-20 12:01:00"),
        )
        self.cycle()
        self.assertEqual(self.pr_tasks(), [("review-comment", "comment:github-followup", "trigger")])
        self.assertNotIn("tapd.example", Path(github_run["prompt_paths"][0]).read_text())
        self.fixture_path.write_text('{"events": []}')
        task = self.submit_pr_answer("comment:github-followup", output="review_comment")
        self.cycle()
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            conn.execute("UPDATE github_actions SET publish_status = 'published' WHERE task_id = ?", (task,))

        (prompt,) = self.cycle()["prompt_paths"]
        docs = [json.loads(path.read_text()) for path in Path(prompt).parent.glob("*.json")]
        events = [event["event_fingerprint"] for doc in docs for event in doc.get("events", [])]
        self.assertIn("tapd-comment:tapd.example/100/3", events)
        self.assertIn("tapd-comment:tapd.example/100/4", events)
        self.submit_pr_answer(
            "tapd-comment:tapd.example/100/3", additional_requests=("tapd-comment:tapd.example/100/4",)
        )
        self.publish()
        self.cycle()
        self.assertEqual(
            self.query("SELECT lifecycle FROM tasks WHERE workstream_id = ? ORDER BY rowid", (PR_WORKSTREAM,)),
            [("completed",), ("completed",)],
        )

    def test_pr_queue_request_classified_before_the_ticket_ends_still_gets_its_answer(self):
        self.start_pr()
        self.scan(comment("3", "@robert-wklken 看看这个", "2026-11-20 12:00:00"))
        self.cycle()
        [(task_id, attempt_id, route_id)] = self.query(
            "SELECT task_id, attempt_id, route_id FROM tasks JOIN attempts USING(task_id) WHERE workstream_id = ?",
            (PR_WORKSTREAM,),
        )
        self.assertEqual(route_id, "classification-result")
        self.client.status[STORY] = "status_3"
        self.scan()
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/3"],
                    "verification": [],
                    "handoff": "Recommend route comment-analysis",
                    "recommended_route": "comment-analysis",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        self.cycle()
        self.assertEqual(self.query("SELECT state FROM github_sources WHERE provider = 'tapd'"), [("closed",)])
        self.cycle()
        self.assertEqual(
            self.query(
                "SELECT t.route_id, t.lifecycle, a.status FROM tasks t JOIN attempts a USING(task_id) "
                "WHERE t.parent_task_id = ? AND t.workstream_id = ?",
                (task_id, PR_WORKSTREAM),
            ),
            [("comment-analysis", "queued", "prepared")],
        )
        self.assertEqual(
            self.query(
                "SELECT notification_type FROM notifications WHERE notification_type LIKE '%authorization_rejected'"
            ),
            [],
        )

    def test_multiple_open_prs_notify_once_and_never_admit_the_request(self):
        self.start_pr()
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources (source_id,repo_id,source_key,source_type,number,state,html_url) "
                "SELECT 'second-source',repo_id,'github:example/backend!11','pull_request',11,'open',? "
                "FROM workstreams WHERE workstream_id = ?",
                (PR_URL[:-2] + "11", PR_WORKSTREAM),
            )
            conn.execute(
                "INSERT INTO workstreams (workstream_id,repo_id,primary_source_id,origin_workstream_id,lifecycle,"
                "created_at,updated_at) SELECT 'github:example/backend!11',repo_id,'second-source',origin_workstream_id,"
                "'completed',created_at,updated_at FROM workstreams WHERE workstream_id = ?",
                (PR_WORKSTREAM,),
            )
        self.scan(comment("3", "@robert-wklken 请分析这个需求", "2026-11-20 12:00:00"))
        self.cycle()
        self.scan()
        self.cycle()
        config = validation.validate_config(self.config_path, skip_external=True)
        publisher = tapd_comments.Publisher(config["tapd"], self.data_dir, client=self.client)
        self.assertEqual(tapd_notifications.deliver_pending(config, publisher=publisher)["delivered"], 1)
        self.assertNotIn("delivered", tapd_notifications.deliver_pending(config, publisher=publisher))
        self.assertIn("多个打开的 PR", self.client.written[-1])
        self.assertEqual(len(self.client.written), 2)
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("completed",)])
        self.assertEqual(
            self.query("SELECT authorization_status FROM github_events WHERE event_fingerprint LIKE '%/3'"),
            [("ignored_context",)],
        )
        self.assertEqual(self.query("SELECT status FROM notifications WHERE channel = 'tapd'"), [("delivered",)])

    def test_github_request_prompt_is_isolated_and_tapd_waits_for_the_pr_queue(self):
        self.start_pr()
        [(ref,)] = self.query("SELECT public_ref FROM workstreams WHERE workstream_id = ?", (TICKET_WORKSTREAM,))
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "github-followup",
                            "event_type": "comment",
                            "source_type": "pull_request",
                            "number": 10,
                            "body": "@robert-bot please analyze the login timeout",
                            "actor_login": "wklken",
                            "state": "open",
                            "url": PR_URL,
                            "pr_author_login": "robert-bot",
                            "metadata": {"dd_workstream": {"origin_workstream_id": ref}},
                        }
                    ]
                }
            )
        )
        outcome = self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks WHERE workstream_id = ?", (PR_WORKSTREAM,)), [(1,)])
        self.scan(comment("3", "@robert-wklken 请分析这个需求", "2026-11-20 12:00:00"))
        self.cycle()
        (prompt,) = outcome["prompt_paths"]
        for path in Path(prompt).parent.iterdir():
            if path.suffix in {".json", ".md"}:
                self.assertNotIn("tapd.example", path.read_text(), path)
                self.assertNotIn("@robert-wklken in body", path.read_text(), path)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks WHERE workstream_id = ?", (PR_WORKSTREAM,)), [(1,)])
        [(recorded,)] = self.query(
            "SELECT json_extract(payload_json, '$.origin_workstream_id') FROM github_events WHERE event_fingerprint = ?",
            ("comment:github-followup",),
        )
        self.assertEqual(recorded, TICKET_WORKSTREAM)

    def test_existing_pr_obeys_both_ci_and_conflict_remediation_policies(self):
        self.start_pr()
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        repo["pr_automation"]["conflict"]["enabled"] = True
        repo["pr_automation"]["ci"].update(enabled=True, check_allowlist=["unit"])
        observed = {
            "state": "open",
            "mergeable": False,
            "head_sha": "abc123",
            "base_sha": "def456",
            "head_branch": self.query("SELECT branch_name FROM attempts")[0][0],
            "base_branch": "master",
            "author_login": repo["github_account"],
            "url": PR_URL,
        }
        with (
            closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn,
            mock.patch.object(pr_health, "fetch_pr_snapshot", return_value=observed) as snapshot,
            mock.patch.object(
                pr_health,
                "collect_ci_observations",
                return_value=[
                    {
                        "source_kind": "workflow_run",
                        "external_id": "101",
                        "attempt_no": 1,
                        "status": "completed",
                        "check_name": "unit",
                        "conclusion": "failure",
                        "evidence_status": "available",
                        "failure_signature": "unit-failure",
                    }
                ],
            ),
        ):
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            decisions = remediation_requests.collect_pr_remediation_decisions(
                conn,
                repo_id,
                repo,
                None,
                "2026-11-20T04:10:00+00:00",
                config=config,
            )
        snapshot.assert_called_once()
        self.assertEqual({event["task_kind"] for event in decisions}, {"ci_remediation", "merge_conflict_remediation"})
        self.assertTrue(all(event["workstream_id"] == PR_WORKSTREAM for event in decisions))

    def test_development_updates_the_existing_branch_in_the_pr_workstream(self):
        self.start_pr()
        self.scan(comment("3", "@robert-wklken please fix the logout bug", "2026-11-20 12:00:00"))
        self.cycle()
        self.assertEqual(
            self.query("SELECT t.workstream_id, t.route_id, a.branch_name FROM tasks t JOIN attempts a USING(task_id)"),
            [
                (TICKET_WORKSTREAM, "new-pr", self.query("SELECT branch_name FROM attempts LIMIT 1")[0][0]),
                (PR_WORKSTREAM, "update-existing-pr", self.query("SELECT branch_name FROM attempts LIMIT 1")[0][0]),
            ],
        )
        self.assertEqual(
            self.query(
                "SELECT gs.source_type, ws.relationship FROM workstream_sources ws "
                "JOIN github_sources gs USING(source_id) WHERE ws.workstream_id = ? ORDER BY source_type",
                (PR_WORKSTREAM,),
            ),
            [("pull_request", "derived_pr"), ("tapd_story", "related")],
        )

        [(task, attempt)] = self.query(
            "SELECT task_id, attempt_id FROM tasks JOIN attempts USING(task_id) WHERE workstream_id = ?",
            (PR_WORKSTREAM,),
        )
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            trusted = scope.for_result(conn, task, attempt, PR_WORKSTREAM, observe=False)
        self.assertIn({"source_type": "pull_request", "number": 10}, trusted["sources"])
        denied = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task,
                "attempt_id": attempt,
                "output_type": "update_existing_pr",
                "submission": {
                    "planned_github_actions": [
                        {"type": "push_existing_pr"},
                        {"type": "comment", "target_url": PR_URL, "body": "updated"},
                    ],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/3"],
                    "verification": [_verification_evidence()],
                    "handoff": "completed",
                    "used_skills": [],
                    "review_input_kind": "request",
                },
            },
        )
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(denied["reason_code"], "action_scope")


class TapdDerivedOperatorReplyTests(TapdPullRequestCase):
    def setUp(self):
        super().setUp()
        self.db = self.data_dir / "dd.sqlite3"
        self.request_fix()
        self.open_pull_request()
        self.assertTrue(self.publish()["ok"])
        self.scan(comment("3", "@robert-wklken please fix the logout bug", "2026-11-20 12:00:00"))
        self.cycle()
        ((self.parent, attempt, self.workspace, self.branch),) = self.query(
            "SELECT t.task_id, a.attempt_id, a.worktree_path, a.branch_name "
            "FROM tasks t JOIN attempts a USING(task_id) WHERE t.workstream_id = ?",
            (PR_WORKSTREAM,),
        )
        recorded = submission.record_result(
            self.db,
            {
                "task_id": self.parent,
                "attempt_id": attempt,
                "output_type": "waiting_for_user",
                "submission": {
                    "planned_github_actions": [{"type": "comment", "body": "Which logout behavior should I use?"}],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/3"],
                    "operator_question": {
                        "kind": "scope_decision",
                        "summary": "Which logout behavior?",
                        "choices": example_choices(),
                    },
                    "verification": [_verification_evidence()],
                    "handoff": "Waiting for logout behavior.",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(recorded["ok"], recorded)
        self.assertTrue(self.publish()["ok"])
        ((self.item_id, self.repo_id),) = self.query("SELECT work_item_id, repo_id FROM work_items")
        self.context = commands.CommandContext(
            actor_kind="operator",
            actor_identity="local-operator",
            allowed_repo_ids=frozenset({self.repo_id}),
            allowed_workers=frozenset({"default"}),
        )
        item = board.get_work_item_detail(self.db, self.item_id)
        self.reply = commands.execute_command(
            self.db,
            self.item_id,
            context=self.context,
            command="reply",
            expected_version=item["version"],
            idempotency_key="confirm-logout",
            body="Clear the session on logout.",
        )
        self.task = self.reply["task_id"]
        ((requested,),) = self.query("SELECT created_at FROM wakeups WHERE task_id=?", (self.task,))
        self.requested = datetime.fromisoformat(requested)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            (metadata,) = conn.execute("SELECT metadata_json FROM github_sources WHERE provider='tapd'").fetchone()
            metadata = json.loads(metadata)
            metadata["tapd"]["observed_at"] = (self.requested - timedelta(seconds=1)).isoformat()
            conn.execute("UPDATE github_sources SET metadata_json=? WHERE provider='tapd'", (json.dumps(metadata),))

    def refresh(self):
        config = validation.validate_config(self.config_path, skip_external=True)
        return tapd_scan.scan_project(
            self.db,
            config["tapd"],
            config["tapd"]["projects"][0],
            {repo["full_name"]: repo for repo in config["repos"]},
            self.client,
            now=(self.requested + timedelta(seconds=1)).isoformat(),
        )

    def attempts(self):
        return self.query("SELECT worktree_path, branch_name FROM attempts WHERE task_id=?", (self.task,))

    def test_reply_uses_fresh_ticket_context_and_keeps_the_pr_workstream(self):
        self.assertEqual(
            self.query("SELECT workstream_id, parent_task_id FROM tasks WHERE task_id=?", (self.task,)),
            [(PR_WORKSTREAM, self.parent)],
        )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False))
        self.cycle()
        self.assertEqual(self.attempts(), [])
        observed = self.refresh()
        self.assertFalse(observed["failures"], observed)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False))
        self.cycle()
        self.assertEqual(self.attempts(), [(self.workspace, self.branch)])
        self.assertEqual(
            self.query("SELECT workstream_id, route_id FROM tasks WHERE task_id=?", (self.task,)),
            [(PR_WORKSTREAM, "update-existing-pr")],
        )
        self.assertEqual(self.query("SELECT status FROM wakeups WHERE task_id=?", (self.task,)), [("consumed",)])
        ((prompt,),) = self.query("SELECT path FROM artifacts WHERE task_id=? AND artifact_type='prompt'", (self.task,))
        text = Path(prompt).read_text()
        self.assertIn("origin_type: tapd", text)
        self.assertIn("Clear the session on logout.", text)
        self.assertIn("Which logout behavior should I use?", text)
        self.assertIn("@robert-wklken please fix the logout bug", text)
        self.assertIn("@robert-wklken in body", text)
        replay = commands.execute_command(
            self.db,
            self.item_id,
            context=self.context,
            command="reply",
            expected_version=self.reply["item"]["version"] - 1,
            idempotency_key="confirm-logout",
            body="Clear the session on logout.",
        )
        self.assertEqual(replay["task_id"], self.task)
        self.cycle()
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(3,)])

    def test_failed_ticket_refresh_does_not_launch_the_pr_reply(self):
        self.client.fail_items.add(STORY)
        observed = self.refresh()
        self.assertTrue(observed["failures"], observed)
        self.cycle()
        self.assertEqual(self.attempts(), [])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(loop.has_runnable_local_work(conn, repo_id=self.repo_id, include_publication=False))

    def test_ended_ticket_stops_the_pr_reply(self):
        self.client.status[STORY] = "status_3"
        self.refresh()
        self.cycle()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (self.task,)), [("failed",)])
        self.assertEqual(
            self.query(
                "SELECT json_extract(metadata_json, '$.reason_code') FROM work_item_events "
                "WHERE event_type='execution_failed'"
            ),
            [("tapd_operator_source_closed",)],
        )

    def test_changed_trusted_user_stops_the_pr_reply(self):
        self.refresh()
        self.configure("bob")
        self.cycle()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (self.task,)), [("failed",)])
        self.assertEqual(
            self.query(
                "SELECT json_extract(metadata_json, '$.reason_code') FROM work_item_events "
                "WHERE event_type='execution_failed'"
            ),
            [("tapd_operator_authorization_unavailable",)],
        )


class TapdPrNoticeTests(TapdPullRequestCase):
    def start_pr(self):
        self.request_fix()
        self.open_pull_request()
        self.assertTrue(self.publish()["ok"])

    def notice_types(self):
        return [kind for (kind,) in self.query("SELECT notification_type FROM notifications WHERE channel = 'tapd'")]

    def deliver(self, now="2026-11-20T04:20:00+00:00"):
        config = validation.validate_config(self.config_path, skip_external=True)
        publisher = tapd_comments.Publisher(config["tapd"], self.data_dir, client=self.client)
        return tapd_notifications.deliver_pending(config, publisher=publisher, now=now)

    def test_repeated_merge_observation_publishes_one_notice(self):
        self.start_pr()
        self.reconcile(MERGED)
        self.reconcile(MERGED)
        self.assertEqual(self.notice_types(), ["tapd_pr_merged"])
        self.assertEqual(self.deliver().get("delivered"), 1)
        self.assertNotIn("delivered", self.deliver())
        self.assertIn("已合并", self.client.written[-1])
        self.assertIn(PR_URL, self.client.written[-1])

    def test_unmerged_close_is_notified_only_if_ticket_is_open(self):
        self.start_pr()
        closed = {**MERGED, "merged": False, "merged_at": None}
        self.reconcile(closed)
        self.reconcile(closed)
        self.assertEqual(self.notice_types(), ["tapd_pr_closed"])
        self.client.status[STORY] = "status_3"
        self.scan()
        self.assertEqual(self.deliver().get("skipped"), 1)
        self.assertEqual(len(self.client.written), 1)
        self.assertEqual(self.query("SELECT status FROM notifications WHERE channel = 'tapd'"), [("skipped",)])

    def test_reviews_of_one_pr_revision_send_only_one_link_reminder(self):
        self.start_pr()
        first, second = "a" * 40, "b" * 40

        def review(review_id, state, commit_id, login, **extra):
            submitted = None if state == "PENDING" else "2026-11-20T04:01:00Z"
            return {
                "id": review_id,
                "state": state,
                "submitted_at": submitted,
                "commit_id": commit_id,
                "user": {"login": login},
                **extra,
            }

        reviews = [
            review(10, "COMMENTED", first, "alice", body="Private review text"),
            review(11, "PENDING", second, "alice", body="Unsubmitted draft"),
            review(12, "COMMENTED", first, "alice", body=""),
            review(13, "APPROVED", first, "bob"),
            review(14, "COMMENTED", second, "Robert-Bot", body="Robert's reply"),
        ]

        def runner(args, **kwargs):
            self.assertEqual(args, ["gh", "api", "repos/example/backend/pulls/10/reviews", "--paginate", "--slurp"])
            return subprocess.CompletedProcess(args, 0, json.dumps([reviews]), "")

        self.reconcile({"state": "open", "html_url": PR_URL}, review_runner=runner)
        self.assertEqual(self.notice_types(), ["tapd_pr_review_submitted"])
        self.assertEqual(self.deliver().get("delivered"), 1)
        self.reconcile({"state": "open", "html_url": PR_URL}, review_runner=runner)
        self.assertNotIn("delivered", self.deliver())
        reviews.append(review(15, "CHANGES_REQUESTED", second, "bob"))
        self.reconcile({"state": "open", "html_url": PR_URL}, review_runner=runner)
        self.assertEqual(self.deliver().get("delivered"), 1)
        self.assertEqual(
            self.client.written[1:],
            [f"robert-wklken：\n\n关联的 PR 有新的评审。\n\n{PR_URL}"] * 2,
        )

    def test_background_scan_replays_final_review_without_other_github_activity(self):
        self.start_pr()
        self.reconcile(MERGED)
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        review = {
            "id": 42,
            "state": "APPROVED",
            "submitted_at": "2026-11-20T04:02:00Z",
            "commit_id": "c" * 40,
            "user": {"login": "alice"},
        }
        session = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps([[review]]), ""))
        session.collect_events.return_value = []
        self.assertIsNotNone(synchronization.scan_repo(config, repo, session, []))
        self.assertEqual(
            session.call_args.args[0],
            [
                "gh",
                "api",
                f"repos/{repo['full_name']}/pulls/10/reviews",
                "--paginate",
                "--slurp",
            ],
        )

        outcome = run_once.run_once(
            self.config_path, dry_run=True, skip_external=True, skip_publish=True, consume_observations=True
        )
        self.assertTrue(outcome["ok"], outcome)
        self.assertCountEqual(self.notice_types(), ["tapd_pr_merged", "tapd_pr_review_submitted"])

    def test_closed_pr_review_read_retries_until_its_last_review_is_observed(self):
        self.start_pr()
        calls = 0

        def runner(args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.CalledProcessError(1, args, stderr="temporary error")
            review = {
                "id": 42,
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-11-20T04:02:00Z",
                "commit_id": "c" * 40,
                "user": {"login": "alice"},
            }
            return subprocess.CompletedProcess(args, 0, json.dumps([[review]]), "")

        self.reconcile(MERGED, review_runner=runner)
        self.reconcile(MERGED, review_runner=runner)
        self.assertEqual(calls, 2)
        self.assertCountEqual(self.notice_types(), ["tapd_pr_merged", "tapd_pr_review_submitted"])

    def test_pending_merge_notice_keeps_tracking_the_ended_ticket(self):
        self.start_pr()
        self.reconcile(MERGED)
        self.client.status[STORY] = "status_3"
        self.scan()
        self.assertEqual(self.deliver().get("skipped"), 1)
        self.assertEqual(len(self.client.written), 1)

    def test_notice_retry_budget_stops_after_three_failures(self):
        self.start_pr()
        self.reconcile(MERGED)
        self.client.reject_writes = 4
        self.assertEqual(self.deliver("2026-11-20T04:20:00+00:00").get("pending"), 1)
        self.assertEqual(self.deliver("2026-11-20T04:20:20+00:00").get("pending"), 1)
        self.assertEqual(self.deliver("2026-11-20T04:20:40+00:00").get("failed"), 1)
        self.assertNotIn("failed", self.deliver("2026-11-20T04:21:00+00:00"))
        self.assertEqual(len(self.client.rejected), 3)

    def test_remediation_outcomes_use_the_episode_and_keep_local_notices(self):
        self.start_pr()
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        repo["pr_automation"]["conflict"]["enabled"] = True
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            conn.row_factory = sqlite3.Row
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            stream = {"workstream_id": PR_WORKSTREAM, "repo_id": repo_id, "number": 10}
            snapshot = {"state": "open", "head_sha": "original", "base_sha": "base", "mergeable": False}
            now = "2026-11-20T04:10:00+00:00"
            pr_remediation.upsert_health_episodes(
                conn,
                repo=repo,
                workstream=stream,
                snapshot=snapshot,
                ci_summary=None,
                now=now,
            )
            (episode,) = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()
            self.assertTrue(
                pr_remediation.record_publish_result(
                    conn,
                    episode_id=episode,
                    publish_status="published",
                    result_head_sha="fixed",
                    now=now,
                )
            )
            for _ in range(2):
                pr_remediation.upsert_health_episodes(
                    conn,
                    repo=repo,
                    workstream=stream,
                    snapshot={**snapshot, "head_sha": "fixed", "mergeable": True},
                    ci_summary=None,
                    now=now,
                )
            pr_remediation.upsert_health_episodes(
                conn,
                repo=repo,
                workstream=stream,
                snapshot={**snapshot, "head_sha": "new-conflict"},
                ci_summary=None,
                now=now,
            )
            (attempt_limit,) = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes WHERE observed_head_sha = 'new-conflict'"
            ).fetchone()
            for _ in range(2):
                pr_remediation.exhaust_episode(conn, episode_id=attempt_limit, reason="max_attempts", now=now)
        self.assertCountEqual(self.notice_types(), ["tapd_remediation_resolved", "tapd_remediation_budget_exhausted"])
        self.assertEqual(
            self.query("SELECT notification_type, status FROM notifications WHERE channel = 'local'"),
            [("pr_remediation_exhausted", "recorded")],
        )
        self.assertEqual(self.deliver().get("delivered"), 2)
        self.assertTrue(all(PR_URL in body for body in self.client.written[1:]))


if __name__ == "__main__":
    unittest.main()

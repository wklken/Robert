"""TAPD development requests open or update one derived PR and answer on the ticket."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest import mock

from robert_agent.adapters.github import sources as github_sources
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.configuration import validation
from robert_agent.delivery.application import scope
from robert_agent.delivery.application import submission, tapd_comments, tapd_notifications
from robert_agent.runtime import cycle as run_once
from robert_agent.work.application import remediation_requests, source_lifecycle
from robert_agent.workflows import pr_health
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

    def reconcile(self, remote_state):
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        with (
            closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn,
            mock.patch.object(github_sources, "read_source_state", return_value=remote_state),
        ):
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            for _ in range(2):
                source_lifecycle.reconcile_remote_source_states(conn, repo_id, repo, None, "2026-11-20T04:10:00+00:00")
            conn.commit()


class TapdNewPullRequestTests(TapdPullRequestCase):
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


if __name__ == "__main__":
    unittest.main()

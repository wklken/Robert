"""One authorized TAPD request delivers independently to each target branch."""

import json
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
from datetime import datetime, timedelta
from unittest import mock

from robert_agent.configuration import validation
from robert_agent.delivery.application import scope, submission
from robert_agent.runtime import cycle as run_once
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.adapters.github import sources as github_sources
from robert_agent.work.application import commands, tapd_scan, source_lifecycle
from robert_agent.work.application import intake
from robert_agent.readmodels import board
from tests.support.repository_cycle import _verification_evidence

from tests.support.tapd import STORY, comment
from tests.support.tapd_cycle import TapdCycleCase
from tests.integration.repository_cycle.test_tapd_pull_requests import MERGED, TapdPullRequestCase


class TapdBackportTests(TapdCycleCase):
    def backport_tasks(self):
        return self.query(
            "SELECT t.task_id,t.workstream_id,t.route_id,a.worktree_path,a.branch_name,t.metadata_json "
            "FROM tasks t JOIN attempts a USING(task_id) ORDER BY t.rowid"
        )

    def test_one_request_creates_two_independent_target_tasks_and_replays_once(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26"))
        result = self.cycle()
        tasks = self.backport_tasks()
        self.assertEqual(len(tasks), 2, result)
        self.assertEqual({row[2] for row in tasks}, {"new-pr"})
        self.assertEqual(len({row[1] for row in tasks}), 2)
        self.assertEqual(len({row[3] for row in tasks}), 2)
        self.assertEqual(len({row[4] for row in tasks}), 2)
        self.assertEqual({json.loads(row[5])["target_base_branch"] for row in tasks}, {"release/1.25", "release/1.26"})
        for path in result["prompt_paths"]:
            prompt = Path(path).read_text()
            self.assertIn("https://github.com/example/backend/pull/10", prompt)
            self.assertIn("target_base_branch: release/", prompt)
        self.scan()
        self.cycle()
        self.cycle()
        self.assertEqual(self.backport_tasks(), tasks)

    def test_untrusted_backport_does_not_create_tasks(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25", author="mallory"))
        self.cycle()
        self.assertEqual(self.backport_tasks(), [])

    def test_quoted_and_code_commands_stay_context_after_scan_and_admission(self):
        history = "cherry-pick #10 to release/1.25"
        contexts = (
            f"<blockquote>{history}</blockquote>",
            f"<pre>{history}</pre>",
            f"<p><code>{history}</code></p>",
            f"\n\n    {history}",
        )
        self.scan(
            *(comment(str(index), "@robert-wklken 请分析历史记录\n" + body) for index, body in enumerate(contexts, 2))
        )
        saved = self.query("SELECT authorization_status,payload_json FROM github_events")
        self.assertEqual(len(saved), len(contexts))
        for authorization, payload in saved:
            self.assertEqual(authorization, "authorized_trigger")
            self.assertIn(history, json.loads(payload)["body"])
            self.assertIsNone(json.loads(payload)["backport_request"])
        self.cycle()
        tasks = self.backport_tasks()
        self.assertTrue(tasks)
        self.assertEqual({task[2] for task in tasks}, {"comment-analysis"})
        self.assertTrue(all(not json.loads(task[5]).get("target_base_branch") for task in tasks))
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM github_events WHERE event_fingerprint LIKE '%:backport:%'"), [(0,)]
        )

    def test_body_command_is_saved_and_admitted_without_quoted_targets(self):
        self.scan(
            comment(
                "2",
                "<p>@robert-wklken cherry-pick #10 to release/1.25, release/1.26</p>"
                "<blockquote>cherry-pick #11 to release/old</blockquote>",
            )
        )
        ((payload,),) = self.query("SELECT payload_json FROM github_events")
        event = json.loads(payload)
        self.assertEqual(event.get("backport_request"), {"source_pr": 10, "targets": ["release/1.25", "release/1.26"]})
        self.cycle()
        tasks = self.backport_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            {json.loads(task[5])["target_base_branch"] for task in tasks}, {"release/1.25", "release/1.26"}
        )
        self.scan()
        self.cycle()
        self.assertEqual(self.backport_tasks(), tasks)

    def test_mixed_html_fenced_commands_stay_context_after_scan_and_admission(self):
        history = "cherry-pick #10 to release/1.25"
        contexts = (
            f"```html\n<span>example</span>\n{history}\n```",
            f"~~~html\n<div>example</div>\n{history}\n~~~",
            f"<p>HTML outside the fence</p>\n```\n{history}\n```",
        )
        self.scan(
            *(comment(str(index), "@robert-wklken 请分析历史记录\n" + body) for index, body in enumerate(contexts, 2))
        )
        saved = self.query("SELECT authorization_status,payload_json FROM github_events")
        self.assertEqual(len(saved), len(contexts))
        for authorization, payload in saved:
            event = json.loads(payload)
            with self.subTest(comment_id=event["comment_id"]):
                self.assertEqual(authorization, "authorized_trigger")
                self.assertIn(history, event["body"])
                self.assertIsNone(event["backport_request"])
        self.cycle()
        tasks = self.backport_tasks()
        self.assertTrue(tasks)
        self.assertEqual({task[2] for task in tasks}, {"comment-analysis"})
        self.assertTrue(all(not json.loads(task[5]).get("target_base_branch") for task in tasks))
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM github_events WHERE event_fingerprint LIKE '%:backport:%'"), [(0,)]
        )

    def test_legacy_event_without_parsed_request_does_not_reparse_display_body(self):
        self.scan(
            comment(
                "2",
                "<p>@robert-wklken 请分析历史记录</p><blockquote>cherry-pick #10 to release/1.25</blockquote>",
            )
        )
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            event_id, payload = conn.execute("SELECT event_id,payload_json FROM github_events").fetchone()
            event = json.loads(payload)
            event.pop("backport_request", None)
            conn.execute("UPDATE github_events SET payload_json=? WHERE event_id=?", (json.dumps(event), event_id))
        self.cycle()
        tasks = self.backport_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0][2], "comment-analysis")
        self.assertFalse(json.loads(tasks[0][5]).get("target_base_branch"))
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM github_events WHERE event_fingerprint LIKE '%:backport:%'"), [(0,)]
        )

    def submit_target(self, task, *, output="new_pr"):
        task_id, stream_id, _route, worktree, branch, metadata = task
        base = json.loads(metadata)["target_base_branch"]
        self._materialize_worktree(worktree, branch)
        subprocess.run(["git", "update-ref", f"refs/remotes/upstream/{base}", "HEAD"], cwd=worktree, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "Carry resource grants",
            ],
            cwd=worktree,
            check=True,
            capture_output=True,
        )
        ((attempt,),) = self.query("SELECT attempt_id FROM attempts WHERE task_id=?", (task_id,))
        ((fingerprint,),) = self.query(
            "SELECT e.event_fingerprint FROM github_events e JOIN task_events te USING(event_id) WHERE te.task_id=?",
            (task_id,),
        )
        actions = [
            {"type": "push_existing_pr"},
            {"type": "open_pr", "title": "Carry resource grants", "body": "Backport resource grants."},
            {"type": "comment", "body": "回移完成。"},
        ]
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task_id,
                "attempt_id": attempt,
                "output_type": output,
                "submission": {
                    "planned_github_actions": actions,
                    "consumed_event_fingerprints": [fingerprint],
                    "verification": [_verification_evidence()],
                    "handoff": "Carried source commits.",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            resolved = scope.for_result(conn, task_id, attempt, stream_id, observe=False)
        self.assertEqual(resolved["base_branch"], base)
        self.assertEqual(
            self.query(
                "SELECT json_extract(metadata_json,'$.base') FROM github_actions WHERE task_id=? AND action_type='open_pr'",
                (task_id,),
            ),
            [(base,)],
        )
        return record

    def publish_targets(self, pairs, *, succeeds=True):
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            for task, number in pairs:
                conn.execute(
                    "UPDATE github_actions SET publish_status='published', target_url=CASE WHEN action_type='open_pr' "
                    "THEN ? ELSE target_url END WHERE task_id=? AND action_type IN ('push_existing_pr','open_pr')",
                    (f"https://github.com/example/backend/pull/{number}", task[0]),
                )
        with (
            mock.patch.object(tapd_client, "client_for", return_value=self.client),
            mock.patch.object(
                github_sources,
                "read_source_state",
                return_value={"state": "open"},
            ),
        ):
            result = run_once.run_once(self.config_path, dry_run=False, skip_external=True, max_dispatches=0)
        self.assertEqual(result["ok"], succeeds, result)

    def test_each_target_publishes_its_own_pr_and_ticket_reply_then_reuses_exact_base(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to master, release/1.25, release/1.26"))
        self.cycle()
        tasks = self.backport_tasks()
        self.assertEqual(len(tasks), 3)
        for task in tasks:
            self.submit_target(task)
        self.publish_targets(list(zip(tasks, (11, 12, 13))))
        self.assertEqual(len(self.client.written), 3)
        self.assertEqual({row[0] for row in self.query("SELECT lifecycle FROM tasks")}, {"completed"})
        root = self.query("SELECT workstream_id FROM work_items")[0][0]
        self.assertEqual(
            self.query("SELECT origin_workstream_id FROM workstreams WHERE workstream_id LIKE 'github:%'"),
            [(root,)] * 3,
        )
        for number in (11, 12, 13):
            self.assertTrue(any(f"/pull/{number}" in body for body in self.client.written))
        self.scan(comment("3", "@robert-wklken cherry-pick #10 to release/1.26", "2026-11-20 12:00:00"))
        self.cycle()
        last = self.backport_tasks()[-1]
        self.assertEqual(last[1], "github:example/backend!13")
        self.assertEqual(last[2], "update-existing-pr")
        self.assertEqual(last[4], tasks[2][4])
        self.assertEqual(json.loads(last[5])["target_base_branch"], "release/1.26")

    def test_accepted_operator_reply_queues_targets_without_an_inherited_update_attempt(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to master"))
        self.cycle()
        original = self.backport_tasks()[0]
        self.submit_target(original)
        self.publish_targets([(original, 11)])
        self.scan(comment("3", "@robert-wklken please fix this bug", "2026-11-20 12:00:00"))
        self.cycle()
        update = self.backport_tasks()[-1]
        ((attempt,),) = self.query("SELECT attempt_id FROM attempts WHERE task_id=?", (update[0],))
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": update[0],
                "attempt_id": attempt,
                "output_type": "waiting_for_user",
                "submission": {
                    "planned_github_actions": [{"type": "comment", "body": "Which target?"}],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/3"],
                    "operator_question": {"kind": "scope_decision", "summary": "Which target?"},
                    "verification": [],
                    "handoff": "Waiting.",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        self.publish_targets([])
        ((item_id, repo_id),) = self.query("SELECT work_item_id,repo_id FROM work_items")
        detail = board.get_work_item_detail(self.data_dir / "dd.sqlite3", item_id)
        reply = commands.execute_command(
            self.data_dir / "dd.sqlite3",
            item_id,
            context=commands.CommandContext("operator", "local", frozenset({repo_id}), frozenset({"default"})),
            command="reply",
            expected_version=detail["version"],
            idempotency_key="confirm-release-targets",
            body="cherry-pick #11 to release/1.25, release/1.26",
        )
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempts WHERE task_id=?", (reply["task_id"],)), [(0,)])
        ((requested,),) = self.query("SELECT created_at FROM wakeups WHERE task_id=?", (reply["task_id"],))
        config = validation.validate_config(self.config_path, skip_external=True)
        tapd_scan.scan_project(
            config["db_path"],
            config["tapd"],
            config["tapd"]["projects"][0],
            {r["full_name"]: r for r in config["repos"]},
            self.client,
            now=(datetime.fromisoformat(requested) + timedelta(seconds=1)).isoformat(),
        )
        self.cycle()
        self.cycle()
        new = self.backport_tasks()[2:]
        self.assertEqual(len(new), 2)
        self.assertEqual({r[2] for r in new}, {"new-pr"})
        self.assertEqual(
            self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (reply["task_id"],)), [("completed",)]
        )
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempts WHERE task_id=?", (reply["task_id"],)), [(0,)])
        self.cycle()
        self.assertEqual(len(self.backport_tasks()), 4)

    def test_partial_dispatch_keeps_every_target_durable_and_reauthorizes_remaining_work(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26, release/1.27"))
        admit = intake.admit_requests

        def admit_first(conn, **kwargs):
            return admit(conn, **{**kwargs, "decisions": kwargs["decisions"][:1]})

        with mock.patch.object(intake, "admit_requests", side_effect=admit_first):
            first = self.cycle()
        self.assertTrue(first["ok"], first)
        self.assertEqual(len(self.backport_tasks()), 1)
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM github_events WHERE json_extract(payload_json,'$.backport') IS NOT NULL"),
            [(3,)],
        )
        self.configure("bob")
        self.cycle()
        self.assertEqual(len(self.backport_tasks()), 1)
        self.assertEqual(
            self.query("SELECT COUNT(*) FROM github_events WHERE authorization_status='ignored_context'"),
            [(2,)],
        )

    def test_one_target_merging_preserves_the_other_running_target_and_ticket(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26"))
        self.cycle()
        first, second = self.backport_tasks()
        self.submit_target(first)
        self.publish_targets([(first, 11)])
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        merged = {
            "state": "closed",
            "merged": True,
            "merged_at": "2026-11-20T04:00:00Z",
            "closed_at": "2026-11-20T04:00:00Z",
            "html_url": "https://github.com/example/backend/pull/11",
        }
        with (
            closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn,
            mock.patch.object(
                github_sources,
                "read_source_state",
                return_value=merged,
            ),
        ):
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            source_lifecycle.reconcile_remote_source_states(conn, repo_id, repo, None, "2026-11-20T04:10:00+00:00")
            conn.commit()
        self.assertEqual(self.query("SELECT completed_at,canceled_at FROM work_items"), [(None, None)])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (second[0],)), [("queued",)])
        self.assertEqual(
            self.query("SELECT active_task_id FROM workstreams WHERE workstream_id=?", (second[1],)), [(second[0],)]
        )

    def test_pr_closing_before_ticket_reply_ends_only_its_opening_target(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26"))
        self.cycle()
        first, second = self.backport_tasks()
        self.submit_target(first)
        self.client.reject_writes = 1
        self.publish_targets([(first, 11)], succeeds=False)
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        closed = {
            "state": "closed",
            "merged": False,
            "closed_at": "2026-11-20T04:00:00Z",
            "html_url": "https://github.com/example/backend/pull/11",
        }
        with (
            closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn,
            mock.patch.object(
                github_sources,
                "read_source_state",
                return_value=closed,
            ),
        ):
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            source_lifecycle.reconcile_remote_source_states(conn, repo_id, repo, None, "2026-11-20T04:10:00+00:00")
            conn.commit()
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (first[0],)), [("canceled",)])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks WHERE task_id=?", (second[0],)), [("queued",)])

    def test_operator_cancel_stops_all_target_tasks(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26"))
        self.cycle()
        ((item_id, repo_id),) = self.query("SELECT work_item_id,repo_id FROM work_items")
        detail = board.get_work_item_detail(self.data_dir / "dd.sqlite3", item_id)
        result = commands.execute_command(
            self.data_dir / "dd.sqlite3",
            item_id,
            context=commands.CommandContext("operator", "local", frozenset({repo_id}), frozenset({"default"})),
            command="cancel",
            expected_version=detail["version"],
            idempotency_key="cancel-all-targets",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("canceled",), ("canceled",)])
        self.scan(comment("3", "@robert-wklken cherry-pick #10 to release/1.27", "2026-11-20 12:00:00"))
        self.cycle()
        self.assertEqual(len(self.backport_tasks()), 2)

    def test_ended_ticket_stops_all_target_tasks_and_rejects_new_targets(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26"))
        self.cycle()
        self.client.status[STORY] = "status_3"
        self.scan(comment("3", "@robert-wklken cherry-pick #10 to release/1.27", "2026-11-20 12:00:00"))
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            ((repo_id,),) = conn.execute("SELECT repo_id FROM repos").fetchall()
            source_lifecycle.reconcile_remote_source_states(
                conn, repo_id, {"full_name": "example/backend"}, None, "2026-11-20T03:10:00+00:00"
            )
            conn.commit()
        self.cycle()
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("canceled",), ("canceled",)])
        self.assertEqual(self.query("SELECT completed_at IS NOT NULL FROM work_items"), [(1,)])
        self.assertEqual(len(self.backport_tasks()), 2)

    def test_new_request_after_target_pr_closes_uses_a_fresh_branch(self):
        self.scan(comment("2", "@robert-wklken cherry-pick #10 to release/1.25"))
        self.cycle()
        original = self.backport_tasks()[0]
        self.submit_target(original)
        self.publish_targets([(original, 11)])
        config = validation.validate_config(self.config_path, skip_external=True)
        (repo,) = config["repos"]
        with (
            closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn,
            mock.patch.object(github_sources, "read_source_state", return_value={"state": "closed", "merged": False}),
        ):
            (repo_id,) = conn.execute("SELECT repo_id FROM repos").fetchone()
            source_lifecycle.reconcile_remote_source_states(conn, repo_id, repo, None, "2026-11-20T04:10:00+00:00")
            conn.commit()
        self.scan(comment("3", "@robert-wklken cherry-pick #10 to release/1.25", "2026-11-20 12:00:00"))
        self.cycle()
        first, second = self.backport_tasks()
        self.assertEqual(second[2], "new-pr")
        self.assertNotEqual(first[3:5], second[3:5])


class LegacyOpeningBackportTests(TapdPullRequestCase):
    def test_merged_master_pr_keeps_an_open_ticket_available_for_release_backports(self):
        self.request_fix()
        self.open_pull_request()
        self.assertTrue(self.publish()["ok"])
        self.reconcile(MERGED)
        self.scan(comment("3", "@robert-wklken cherry-pick #10 to release/1.25, release/1.26", "2026-11-20 12:00:00"))
        self.cycle()
        self.assertEqual(
            self.query("SELECT route_id,json_extract(metadata_json,'$.target_base_branch') FROM tasks ORDER BY rowid"),
            [("new-pr", None), ("new-pr", "release/1.25"), ("new-pr", "release/1.26")],
        )

    def test_target_waits_for_its_legacy_opening_reply_while_another_base_can_run(self):
        original = self.request_fix()
        self.open_pull_request()
        self.client.reject_writes = 1
        self.assertFalse(self.publish()["ok"])
        self.scan(comment("3", "@robert-wklken cherry-pick #10 to master, release/1.25", "2026-11-20 12:00:00"))
        self.cycle()
        self.assertEqual(
            self.query(
                "SELECT route_id,json_extract(metadata_json,'$.target_base_branch') FROM tasks WHERE task_id!=?",
                (original,),
            ),
            [("new-pr", "release/1.25")],
        )

"""TAPD requests saved by the scan lane are admitted once under current authority and answered on the ticket."""

from contextlib import closing
import re
import sqlite3
import unittest
from unittest import mock

from robert_agent.adapters.github import budget as github_budget
from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.configuration import validation
from robert_agent.delivery.application import lane as delivery_lane
from robert_agent.delivery.application import notifications as notify
from robert_agent.delivery.application import submission, tapd_comments, tapd_notifications
from robert_agent.runtime import cycle as run_once
from robert_agent.work import tapd_policy
from robert_agent.work.application import source_lifecycle
from tests.support.tapd import STORY, comment
from tests.support.tapd_cycle import TapdCycleCase


class TapdAdmissionTests(TapdCycleCase):
    def test_trusted_mention_creates_one_tapd_task_with_a_public_reference(self):
        self.scan(comment("1", "<p>@robert-wklken 请分析一下这个需求</p>"))
        self.cycle()
        self.cycle()
        tasks = self.query(
            "SELECT w.workstream_id, w.public_ref, t.route_id FROM tasks t JOIN workstreams w USING(workstream_id)"
        )
        self.assertEqual(len(tasks), 1)
        workstream_id, public_ref, route_id = tasks[0]
        self.assertTrue(workstream_id.startswith("tapd:"))
        self.assertRegex(public_ref, re.compile(r"^rbt-[0-9a-f]{12}$"))
        self.assertEqual(route_id, "comment-analysis")
        self.assertEqual(self.query("SELECT origin_type FROM work_items"), [("tapd",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM task_events"), [(1,)])
        (prompt,) = self.data_dir.glob("artifacts/*/prompt.md")
        text = prompt.read_text(encoding="utf-8")
        self.assertIn("tapd-request", text)
        self.assertIn("@robert-wklken in body", text)
        self.assertIn("Keep TAPD out of GitHub", text)

    def test_published_reply_completes_the_request_and_is_never_a_new_request(self):
        self.scan(comment("1", "<p>@robert-wklken 请分析一下这个需求</p>"))
        self.cycle()
        ((task_id, attempt_id),) = self.query("SELECT task_id, attempt_id FROM attempts")
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "submission": {
                    "planned_github_actions": [{"type": "comment", "body": "结论：可以做，@robert-wklken 已确认"}],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
                    "verification": [],
                    "handoff": "answered",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with mock.patch.object(tapd_client, "client_for", return_value=self.client):
            outcome = run_once.run_once(self.config_path, dry_run=False, skip_external=True)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(len(self.client.written), 1)
        self.assertTrue(self.client.written[0].startswith("robert-wklken：\n\n结论：可以做，robert-wklken 已确认"))
        self.assertEqual(
            self.query("SELECT publish_status, external_id, target_url FROM github_actions"),
            [("published", "9001", "https://tapd.example/100/prong/stories/view/1100")],
        )
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("completed",)])
        self.scan()
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(1,)])
        self.assertEqual(
            self.query("SELECT authorization_status FROM github_events WHERE event_fingerprint LIKE '%/9001'"),
            [("context_only",)],
        )

    def test_mention_during_execution_waits_for_the_next_round(self):
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        self.cycle()
        self.scan(comment("2", "@robert-wklken 也分析一下兼容性", "2026-11-20 10:20:00"))
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(1,)])
        ((task_id, attempt_id),) = self.query("SELECT task_id, attempt_id FROM attempts")
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "submission": {
                    "planned_github_actions": [{"type": "comment", "body": "结论：可以做"}],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
                    "verification": [],
                    "handoff": "answered",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with mock.patch.object(tapd_client, "client_for", return_value=self.client):
            self.assertTrue(run_once.run_once(self.config_path, dry_run=False, skip_external=True)["ok"])
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(2,)])
        self.assertEqual(
            self.query(
                "SELECT e.event_fingerprint FROM task_events te JOIN github_events e USING(event_id) "
                "WHERE te.task_id != '" + task_id + "'"
            ),
            [("tapd-comment:tapd.example/100/2",)],
        )

    def test_only_a_marked_reply_resumes_a_waiting_request(self):
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        self.cycle()
        ((task_id, attempt_id),) = self.query("SELECT task_id, attempt_id FROM attempts")
        record = submission.record_result(
            self.data_dir / "dd.sqlite3",
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "waiting_for_user",
                "submission": {
                    "planned_github_actions": [{"type": "comment", "body": "需要确认范围：只看接口还是包括前端？"}],
                    "consumed_event_fingerprints": ["tapd-comment:tapd.example/100/1"],
                    "operator_question": {"kind": "scope_decision", "summary": "接口还是前端"},
                    "verification": [],
                    "handoff": "asked",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with mock.patch.object(tapd_client, "client_for", return_value=self.client):
            self.assertTrue(run_once.run_once(self.config_path, dry_run=False, skip_external=True)["ok"])
        self.assertEqual(self.query("SELECT lifecycle FROM workstreams"), [("waiting_for_user",)])
        self.scan(comment("2", "只看接口", "2026-11-20 11:10:00"))
        self.cycle()
        self.assertEqual(self.query("SELECT lifecycle FROM workstreams"), [("waiting_for_user",)])
        self.scan(comment("3", "@robert-wklken 只看接口", "2026-11-20 11:20:00"))
        self.cycle()
        self.assertEqual(self.query("SELECT lifecycle FROM workstreams"), [("active",)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempts"), [(2,)])

    def test_ended_ticket_stops_its_request_and_refuses_queued_ones(self):
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        self.cycle()
        self.client.status[STORY] = "status_3"
        self.scan(comment("2", "@robert-wklken 再看看", "2026-11-20 10:30:00"))

        def no_github(*_args, **_kwargs):
            raise AssertionError("a TAPD ticket state is read by the scan lane, not from GitHub")

        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            ((repo_id,),) = conn.execute("SELECT repo_id FROM repos").fetchall()
            reconciled = source_lifecycle.reconcile_remote_source_states(
                conn, repo_id, {"full_name": "example/backend"}, no_github, "2026-11-20T03:10:00+00:00"
            )
            conn.commit()
        self.assertEqual(reconciled, 1)
        self.assertEqual(self.query("SELECT lifecycle FROM workstreams"), [("completed",)])
        self.assertEqual(self.query("SELECT lifecycle FROM tasks"), [("canceled",)])
        self.assertEqual(self.query("SELECT completed_at IS NOT NULL FROM work_items"), [(1,)])
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM attempts"), [(1,)])
        self.assertEqual(
            self.query("SELECT authorization_status FROM github_events WHERE event_fingerprint LIKE '%/2'"),
            [("ignored_current_state_closed",)],
        )

    def test_code_change_request_from_tapd_opens_a_pull_request_route(self):
        self.scan(comment("2", "@robert-wklken please fix this bug"))
        self.cycle()
        self.assertEqual(self.query("SELECT route_id FROM tasks"), [("new-pr",)])
        ((branch,),) = self.query("SELECT branch_name FROM attempts")
        self.assertNotIn("1100", branch)
        self.assertRegex(branch, r"rbt-[0-9a-f]{12}")

    def test_request_that_lost_authority_is_recorded_and_never_runs(self):
        self.scan(comment("3", "@robert-wklken 帮忙看看"))
        self.configure("bob")
        self.cycle()
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(self.query("SELECT authorization_status FROM github_events"), [("ignored_context",)])


class TapdNoticeTests(TapdCycleCase):
    T0 = "2026-11-20T03:20:00+00:00"

    def deliver(self, now=T0):
        config = validation.validate_config(self.config_path, skip_external=True)
        publisher = tapd_comments.Publisher(config["tapd"], config["data_dir"], client=self.client)
        return tapd_notifications.deliver_pending(config, publisher=publisher, now=now)

    def notices(self):
        return self.query("SELECT status, json_extract(metadata_json, '$.tapd_delivery.status') FROM notifications")

    def test_label_notice_is_delivered_once_with_a_fixed_body(self):
        self.client.labels[STORY] = "其他"
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        config = validation.validate_config(self.config_path, skip_external=True)
        notify.deliver_pending(config["db_path"], config=config, now=self.T0)
        self.assertEqual(self.notices(), [("pending", None)])
        self.deliver()
        self.deliver()
        self.assertEqual(len(self.client.written), 1)
        body = self.client.written[0]
        parsed = tapd_policy.parse_comment(body, "robert-wklken")
        self.assertTrue(parsed.automatic)
        self.assertFalse(parsed.mentions)
        self.assertIn("repo-a", body)
        self.assertEqual(self.notices(), [("delivered", "delivered")])
        self.scan()
        self.assertEqual(len(self.query("SELECT 1 FROM notifications")), 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM github_events"), [(0,)])

    def test_failed_delivery_stops_at_the_shared_retry_limit(self):
        self.client.labels[STORY] = ""
        self.client.write_error = "command_failed"
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        for now in (self.T0, self.T0, "2026-11-20T03:21:00+00:00", "2026-11-20T03:23:00+00:00"):
            self.deliver(now)
        self.deliver("2026-11-20T03:30:00+00:00")
        self.assertEqual(len(self.client.written), 3)
        self.assertEqual(self.notices(), [("failed", "failed")])

    def test_notice_for_an_ended_ticket_is_skipped(self):
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        self.cycle()
        self.client.labels[STORY] = ""
        self.scan(comment("2", "@robert-wklken 再看看", "2026-11-20 10:30:00"))
        self.client.status[STORY] = "status_3"
        self.scan()
        self.deliver()
        self.assertEqual(self.client.written, [])
        self.assertEqual(self.notices(), [("skipped", "skipped")])

    def test_publication_lane_delivers_tapd_notices(self):
        self.client.labels[STORY] = ""
        self.scan(comment("1", "@robert-wklken 请分析这个需求"))
        with (
            mock.patch.object(tapd_client, "client_for", return_value=self.client),
            mock.patch.object(github_budget, "budget", return_value=False),
        ):
            delivery_lane.publish(self.config_path)
        self.assertEqual(len(self.client.written), 1)


if __name__ == "__main__":
    unittest.main()

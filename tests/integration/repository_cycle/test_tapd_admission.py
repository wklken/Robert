"""TAPD requests saved by the scan lane are admitted once under current authority."""

from contextlib import closing
import json
import re
import sqlite3
import unittest
from unittest import mock

from robert_agent.adapters.tapd import client as tapd_client
from robert_agent.configuration import validation
from robert_agent.delivery.application import submission
from robert_agent.runtime import cycle as run_once
from robert_agent.work.application import tapd_scan
from tests.support import repository_cycle as pipeline_fixture
from tests.support.tapd import FakeClient, comment

TAPD_CONFIG = """tapd:
  enabled: true
  mention_name: robert-wklken
  projects:
  - host: tapd.example
    workspace_id: "100"
    trusted_user: {trusted}
    accept_after: "2026-11-20T01:00:00+00:00"
    label_rules:
    - label: repo-a
      repository: example/backend
"""


class TapdAdmissionTests(pipeline_fixture.RepositoryCycleFixture):
    def setUp(self):
        super().setUp()
        self.base_config = self.config_path.read_text(encoding="utf-8")
        self.configure("alice")
        self.fixture_path.write_text(json.dumps({"events": []}), encoding="utf-8")
        self.client = FakeClient()
        self.cycle()

    def configure(self, trusted):
        self.config_path.write_text(self.base_config + TAPD_CONFIG.format(trusted=trusted), encoding="utf-8")

    def cycle(self):
        outcome = run_once.run_once(self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(outcome["ok"], outcome)
        return outcome

    def scan(self, *comments):
        self.client.comments += comments
        config = validation.validate_config(self.config_path, skip_external=True)
        settings = config["tapd"]
        repos = {repo["full_name"]: repo for repo in config["repos"]}
        return tapd_scan.scan_project(
            config["db_path"], settings, settings["projects"][0], repos, self.client, now="2026-11-20T03:00:00+00:00"
        )

    def query(self, sql):
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            return [tuple(row) for row in conn.execute(sql).fetchall()]

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
        self.assertIn("Code changes are not enabled for TAPD requests", text)

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

    def test_code_change_request_from_tapd_goes_to_classification(self):
        self.scan(comment("2", "@robert-wklken 请修复这个报错并提交 PR"))
        self.cycle()
        self.assertEqual(self.query("SELECT route_id FROM tasks"), [("classification-result",)])

    def test_request_that_lost_authority_is_recorded_and_never_runs(self):
        self.scan(comment("3", "@robert-wklken 帮忙看看"))
        self.configure("bob")
        self.cycle()
        self.cycle()
        self.assertEqual(self.query("SELECT COUNT(*) FROM tasks"), [(0,)])
        self.assertEqual(self.query("SELECT authorization_status FROM github_events"), [("ignored_context",)])


if __name__ == "__main__":
    unittest.main()

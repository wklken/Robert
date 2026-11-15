"""TAPD requests saved by the scan lane are admitted once under current authority."""

from contextlib import closing
import json
import re
import sqlite3
import unittest

from robert_agent.configuration import validation
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

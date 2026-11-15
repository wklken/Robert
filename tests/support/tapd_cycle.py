"""Repository cycles over a configured TAPD project with an in-memory TAPD client."""

from contextlib import closing
import json
import sqlite3

from robert_agent.configuration import validation
from robert_agent.runtime import cycle as run_once
from robert_agent.work.application import tapd_scan
from tests.support import repository_cycle as pipeline_fixture
from tests.support.tapd import FakeClient

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


class TapdCycleCase(pipeline_fixture.RepositoryCycleFixture):
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

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]

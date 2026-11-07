"""Shared fixtures for attempt context tests."""

from robert_agent.workflows import routing as workflow_routing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from tests.support import PACKAGE_ROOT
from robert_agent.workflows import routes as route
from robert_agent.runtime import cycle as run_once
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import preparation
from robert_agent.work.application import source_events
from robert_agent.foundation import values


class AttemptContextFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.repo = {
            "full_name": "example/backend",
            "github_account": "robot",
            "trusted_actors": ["human"],
            "default_base_branch": "main",
            "repo_root": str(self.root / "repo"),
            "worktree_root": str(self.root / "worktrees"),
        }
        Path(self.repo["repo_root"]).mkdir()
        (Path(self.repo["repo_root"]) / ".git").mkdir()
        Path(self.repo["worktree_root"]).mkdir()
        config = self.root / "config.yml"
        from robert_agent.operations.initialize import init_config
        import yaml

        init_config(
            config,
            {
                "repo": self.repo["full_name"],
                "repo_path": self.repo["repo_root"],
                "worker": "cbc",
                "github_account": "robot",
                "trusted_actor": "human",
            },
        )
        document = yaml.safe_load(config.read_text())
        document.update(data_dir=str(self.data), database="test.sqlite3")
        document["repos"][0]["worktrees"] = self.repo["worktree_root"]
        config.write_text(yaml.safe_dump(document))
        fixture = self.root / "events.json"
        fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "1",
                            "number": 1,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "human",
                            "body": "@robot fix empty input",
                            "intent": "bug_fix",
                            "state": "open",
                            "url": "https://github.com/example/backend/issues/1",
                        }
                    ]
                }
            )
        )
        result = run_once.run_once(
            config,
            workflow_path=PACKAGE_ROOT / "resources/workflow.yml",
            fixture_path=fixture,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        self.db = self.data / "test.sqlite3"
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.task_id, self.attempt_id = self.conn.execute("SELECT task_id,attempt_id FROM attempts").fetchone()
        self.stream = self.conn.execute("SELECT workstream_id FROM tasks").fetchone()[0]
        self.prompt = Path(result["prompt_paths"][0])
        self.route = deepcopy(route.ROUTES["update-existing-pr"])
        self.route["confidence"] = "high"
        self.prepare = patch.object(
            assigned_workspace,
            "prepare_worktree",
            return_value={
                "ok": True,
                "worktree_path": str(self.root / "worktree"),
                "branch_name": "fix",
            },
        )
        self.prepare_mock = self.prepare.start()
        self.addCleanup(self.prepare.stop)
        self.effective = patch.object(workflow_routing, "effective_route", return_value=self.route)
        self.effective.start()
        self.addCleanup(self.effective.stop)

    def add_event(self, number=2):
        event = {
            "event_fingerprint": f"comment:{number}",
            "event_type": "comment",
            "source_type": "pull_request",
            "source_key": "example/backend!2",
            "number": 2,
            "workstream_id": self.stream,
            "actor_login": "human",
            "body": f"@robot follow-up {number}",
            "intent": "bug_fix",
            "has_open_dd_pr": True,
            "url": "https://github.com/example/backend/pull/2",
        }
        source_events.insert_source_and_event(
            self.conn, values.repository_id("example/backend"), event, values.utc_now()
        )
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES (?,?,?,?)",
            (
                self.task_id,
                values.github_event_id(event["event_fingerprint"]),
                "context",
                values.utc_now(),
            ),
        )
        self.conn.commit()
        return event

    def refresh(self, event):
        return preparation.refresh_prepared_task(
            self.conn, self.data, self.db, self.repo, event, values.utc_now(), True
        )

    def artifacts(self):
        return self.conn.execute(
            "SELECT artifact_type,path,bytes FROM artifacts WHERE attempt_id=? AND artifact_type IN ('prompt','github_context_md','github_context_json')",
            (self.attempt_id,),
        ).fetchall()

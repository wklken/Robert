"""Shared fixtures for github board recovery tests."""

from robert_agent.work import scan_batches as scan_batches
from robert_agent.adapters.github import client as github_client
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import unittest
from tests.support import board_control_integration
from robert_agent.runtime import cycle as run_once
from robert_agent.work.application import commands as work_items
from robert_agent.foundation import values


class GitHubBoardRecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = board_control_integration.BoardControlIntegrationFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db_path
        event = {
            "number": 70,
            "source_type": "issue",
            "event_type": "assigned",
            "id": "70",
            "assigned_to": "robot",
            "assignment_actor_login": "owner",
            "actor_login": "owner",
            "title": "Draw diagrams",
            "body": "",
            "url": "https://github.com/example/repo/issues/70",
        }
        source_fixture = self.fixture.root / "source.json"
        source_fixture.write_text(json.dumps({"events": [event]}))
        result = run_once.run_once(
            self.fixture.config_path,
            fixture_path=source_fixture,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.db)) as c, c:
            self.parent = c.execute("SELECT task_id FROM tasks").fetchone()[0]
            self.original = {
                p: Path(p).read_bytes()
                for (p,) in c.execute("SELECT path FROM artifacts")
            }
            c.execute(
                "UPDATE tasks SET lifecycle='waiting_for_user' WHERE task_id=?",
                (self.parent,),
            )
            c.execute(
                "UPDATE attempts SET status='completed',finished_at=? WHERE task_id=?",
                (values.utc_now(), self.parent),
            )
            c.execute("UPDATE workstreams SET lifecycle='waiting_for_user'")
            self.item = work_items.resolve_work_item_for_workstream(
                c, "github:example/repo#70"
            )
            for (decision,) in c.execute(
                "SELECT event_id FROM work_item_events WHERE event_type='operator_decision_required'"
            ).fetchall():
                work_items.record_system_event(
                    c,
                    self.item["work_item_id"],
                    event_type="operator_approved",
                    idempotency_key="accept-" + decision,
                    resolves_event_id=decision,
                    now=values.utc_now(),
                )
            work_items.record_system_event(
                c,
                self.item["work_item_id"],
                event_type="operator_question",
                idempotency_key="question",
                body="Which architecture?",
                now=values.utc_now(),
            )
        self.reply = work_items.execute_command(
            self.db,
            self.item["work_item_id"],
            context=self.fixture.context,
            command="reply",
            expected_version=self.item["version"],
            idempotency_key="reply",
            body="Read the full issue body and draw the architecture.",
        )
        self.task_id = self.reply["task_id"]

    def read(self, payload=None, *, fresh=True):
        payload = (
            payload
            if payload is not None
            else {
                "body": "Analyze the architecture and draw sequence diagrams.",
                "title": "Draw diagrams",
                "state": "open",
                "updated_at": "2026-10-31T01:00:00Z",
            }
        )
        with closing(sqlite3.connect(self.db)) as c, c:
            requested = c.execute(
                "SELECT created_at FROM wakeups WHERE task_id=?", (self.task_id,)
            ).fetchone()[0]
            when = datetime.fromisoformat(requested).timestamp() + (1 if fresh else -1)
            scan_batches.save_reads(
                c,
                self.fixture.repo_id,
                {
                    github_client.command_key(
                        ["gh", "api", "repos/example/repo/issues/70"]
                    ): json.dumps(payload)
                },
                when,
            )

    def cycle(self):
        return self.fixture._run_local_cycle()

"""Shared source coordinator fixture; contains no test methods."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from robert_agent.adapters.processes.source_model import SourceModelOutcome, SourceModelProfile
from robert_agent.application.source_cycle import advance_source
from robert_agent.configuration.validation import validate_config
from robert_agent.delivery.application import submission
from robert_agent.execution.application import dispatch, monitor, source_models
from robert_agent.foundation import values
from robert_agent.persistence import database, transactions
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import source_observations
from robert_agent.work import tapd_authority as facts


class SourceCycleFixture(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.db = self.root / "state.sqlite"
        init_database(self.db)
        self.ticket = facts.TicketIdentity("tapd.example", "001", "bug", "90001")
        self.rules = []
        self.comments = []
        self.terminal = False
        self.started = []
        self.model_calls = []
        self.factory_calls = 0
        self.model_kind = "selection"
        self.stop_model = False
        self.model_unavailable = False
        self.profile = SourceModelProfile("mock-model", Path("/controlled/claude"), "model", "high", 10, ())

    def config(self, *, two=False, development=False):
        names = ["org/alpha", "org/beta"] if two else ["org/alpha"]
        self.rules = (
            [
                {
                    "id": "dev",
                    "assignee": "owner",
                    "label": "robot",
                    "repo": names[0],
                    "kinds": ["bug"],
                    "repair": {"ci": False, "conflict": False},
                }
            ]
            if development
            else []
        )
        repos = []
        for name in names:
            checkout = self.root / name.rsplit("/", 1)[1]
            (checkout / ".git").mkdir(parents=True)
            (checkout / "README.md").write_text(f"Evidence from {name}\n")
            repos.append(
                {
                    "full_name": name,
                    "checkout": str(checkout),
                    "worktrees": str(self.root / "trees" / checkout.name),
                    "default_branch": "main",
                    "trusted_actors": ["owner"],
                }
            )
        raw = {
            "version": 2,
            "data_dir": str(self.root / "data"),
            "database": "state.sqlite",
            "github": {"account": "bot"},
            "default_worker": "mock-worker",
            "workers": {"mock-worker": {"adapter": "claude", "command": "claude", "model": "model", "effort": "high"}},
            "repos": repos,
            "tapd": {
                "enabled": False,
                "host": self.ticket.host,
                "workspace_id": None,
                "bot_name": "test-helper",
                "reply_name": "test-desk",
                "api_timezone": "UTC",
                "executable": "/controlled/tapd",
                "cwd": str(self.root),
                "projects": [
                    {
                        "workspace_id": self.ticket.workspace_id,
                        "trusted_author": "requester",
                        "repositories": names,
                        "mentions": {"enabled": True, "activated_after": "2026-11-01T00:00:00+00:00"},
                        "rules": self.rules,
                    }
                ],
            },
        }
        del raw["tapd"]["workspace_id"]
        config_path = self.root / "config.yml"
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        self.configured = validate_config(config_path, skip_external=True)
        self.assertTrue(self.configured["ok"], self.configured)
        self.assertIsInstance(self.configured["workers"], list)
        self.assertIsInstance(self.configured["default_worker"], dict)
        self.configured["tapd"]["enabled"] = True  # Only the test injects independently attested typed facts.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for repo in self.configured["repos"]:
                conn.execute(
                    "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        values.repository_id(repo["full_name"]),
                        repo["full_name"],
                        repo["github_account"],
                        repo["default_base_branch"],
                        repo["repo_root"],
                        repo["worktree_root"],
                    ),
                )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            source_observations.ensure_candidate(
                conn,
                source_id=self.ticket.source_id,
                host=self.ticket.host,
                workspace_id=self.ticket.workspace_id,
                kind=self.ticket.kind,
                external_id=self.ticket.external_id,
            )
        if not development:
            self.comments = [self.comment("1")]

    def comment(self, identifier, *, marked=True, suffix=""):
        body = ("@test-helper " if marked else "") + "Please investigate " + identifier + suffix
        created = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        version = facts.CommentVersion(self.ticket, "bug", identifier, created, None, sha256(body.encode()).hexdigest())
        return facts.CommentObservation(
            version,
            body,
            "requester",
            facts.CreatorEvidence(version, "requester", "creator"),
            facts.OwnBodyEvidence(version, "requester", "body"),
        )

    def factory(self, **_):
        self.factory_calls += 1
        return object()

    def provider(self, _client, ticket, **_):
        item = facts.TicketFacts(
            ticket,
            "Ticket",
            "Complete frozen ticket",
            "closed" if self.terminal else "open",
            ("owner",) if self.rules else (),
            ("robot",) if self.rules else (),
            False,
            ("closed",),
        )
        return facts.TicketObservation(item, values.utc_now(), tuple(self.comments), "complete")

    def launch(self, **options):
        self.started.append(options)
        self.assertIn("Complete frozen ticket", options["prompt_path"].read_text())
        return {
            "ok": True,
            "status": "running",
            "pid": 1234567,
            "task_id": options["task_id"],
            "attempt_id": options["attempt_id"],
        }

    def model(self, _profile, prompt, stop, started):
        identity = {
            "pid": 1234567,
            "process_group_id": 1234567,
            "identity_source": "proc",
            "process_start_token": "model-start",
            "command_fingerprint": "a" * 64,
        }
        self.assertTrue(started(identity))
        envelope = json.loads(prompt)
        envelope.pop("prompt")
        self.model_calls.append(envelope["run_id"])
        if self.stop_model:
            return SourceModelOutcome("process_attention")
        if len(self.model_calls) == 1 and self.model_kind == "question":
            output = {"kind": "question", "question": "Which component should I inspect?"}
        else:
            refs = [
                {
                    "repo_id": values.repository_id(repo["full_name"]),
                    "path": "README.md",
                    "revision": "sha256:" + sha256((Path(repo["repo_root"]) / "README.md").read_bytes()).hexdigest(),
                }
                for repo in self.configured["repos"]
            ]
            if len(self.model_calls) == 1:
                output = {"kind": "selection", "repositories": [ref["repo_id"] for ref in refs], "references": refs}
            else:
                output = {"kind": "answer", "answer": "The requested behavior is documented.", "references": refs}
        if output["kind"] != "selection":
            output["handled_event_fingerprints"] = [self.comments[0].version.event_id]
        return SourceModelOutcome("read", json.dumps({**envelope, **output}).encode(), "group_exited", identity)

    def tick(self, expected, *, paced=False, **options):
        if not paced:
            # Most cases drive each eligible transition without sleeping for runtime pacing.
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE tapd_coordination SET next_due_at='2000-01-01T00:00:00+00:00'")
        with (
            patch.object(dispatch.dispatch, "dispatch_worker", side_effect=self.launch),
            patch.object(monitor.attempt_process, "dispatch_status", return_value={"status": "dead"}),
            patch.object(
                source_models,
                "select_worker",
                side_effect=ValueError("unsupported") if self.model_unavailable else None,
                return_value=self.profile,
            ),
        ):
            outcome = advance_source(
                self.db,
                self.root,
                self.configured,
                owner="test-coordinator",
                client_factory=self.factory,
                observation_provider=self.provider,
                model_runner=self.model,
                **options,
            )
        self.assertEqual(outcome.status, expected, outcome)
        return outcome

    def read(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, parameters).fetchall()

    def submit(self, output, **extra):
        started = self.started[-1]
        pending = self.read(
            "SELECT e.event_fingerprint FROM task_events b JOIN source_events e ON e.event_id=b.event_id"
            " WHERE b.task_id=? AND b.relationship='trigger' ORDER BY e.event_fingerprint",
            (started["task_id"],),
        )
        result = submission.record_result(
            self.db,
            {
                "task_id": started["task_id"],
                "attempt_id": started["attempt_id"],
                "output_type": output,
                "submission": {
                    "planned_actions": [],
                    "consumed_event_fingerprints": [row[0] for row in pending],
                    "used_skills": [],
                    "verification": [],
                    "handoff": "Verified repository scope",
                    **extra,
                },
            },
        )
        self.assertEqual(result["status"], "recorded", result)

    @staticmethod
    def git(directory, *args):
        return subprocess.run(["git", *args], cwd=directory, text=True, capture_output=True, check=True).stdout.strip()

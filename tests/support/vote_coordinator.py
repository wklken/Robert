"""Shared fixtures for vote coordinator tests."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import yaml
from tests.support import review_coordinator as review_fixture
from robert_agent.workflows.vote import coordinator as vote_coordinator
from robert_agent.workflows.vote import files as vote_files
from robert_agent.adapters.sqlite import vote_rounds as vote_store
from robert_agent.delivery.application import publish


class VoteCoordinatorFixture(unittest.TestCase):
    def setUp(self):
        self.fixture = review_fixture.ReviewCoordinatorFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.db = self.fixture.db
        config = yaml.safe_load(self.fixture.config_path.read_text())
        config["routes"] = {
            "vote": {
                "worker": "codex",
                "multi_agent_vote": {"enabled": True, "workers": ["codex", "claude"]},
            }
        }
        self.fixture.config_path.write_text(yaml.safe_dump(config))
        self.calls = []
        self.fail_worker = None
        self.clarify = False
        self.need_code = False
        self.issue = True
        self.comment_body = "Choose B for lower latency"

    def event(self, identifier="first", actor="maintainer"):
        return {
            **self.fixture.event(identifier, actor=actor),
            "intent": "vote",
            "source_type": "issue" if self.issue else "pull_request",
            "body": "@robert-bot 投票：A 轮询，B webhook，按可靠性和维护成本比较。",
            "title": "Choose an event delivery mechanism",
        }

    def source(self, *args):
        return {
            **self.fixture.pr("example/backend", 42),
            "title": "Decision",
            "body": "A or B",
            "discussion": [],
        }

    def launcher(self, **kwargs):
        output = Path(kwargs["trusted_environment"]["ROBERT_VOTE_OUTPUT_DIR"])
        prompt = Path(kwargs["prompt_path"])
        manifest = json.loads((prompt.parent / "context" / "manifest.json").read_text())
        stage = (
            "prepare"
            if prompt.name.startswith("prepare-")
            else "vote"
            if prompt.name == "voter-prompt.md"
            else "synthesis"
        )
        worker = kwargs["worker_agent_config"]["name"]
        self.calls.append((stage, worker, prompt.read_bytes(), kwargs["model"]))
        if stage == "vote" and worker == self.fail_worker:
            return {"ok": False, "safe_error": "model unavailable"}
        value = {
            "schema_version": 1,
            "round_id": manifest["round_id"],
            "snapshot_id": manifest["snapshot_id"],
        }
        if stage == "prepare":
            value.update(
                status="ready",
                question="A or B?",
                options=[
                    {"id": "A", "description": "Polling", "origin": "user"},
                    {"id": "B", "description": "Webhook", "origin": "user"},
                ],
                criteria=["Reliability"],
                constraints=[],
                unknowns=[],
            )
            if self.clarify:
                value.update(status="needs_clarification", question="What is the latency target?")
            if self.need_code and not manifest.get("source_worktree"):
                value.update(status="needs_code", question="Need source to compare")
            name = "prepared-request.json"
        elif stage == "vote":
            value.update(
                ballot_spec_sha256=manifest["ballot_spec_sha256"],
                choice_id="B" if worker == "codex" else "A",
                reason_summary="reason from " + worker,
                evidence_refs=["source-1"],
                tradeoffs=[],
                uncertainties=[],
            )
            name = "ballot.json"
        else:
            # The input filename embeds the actual byte hash given to the model.
            candidates = list((prompt.parent / "synthesis").glob("inputs-*.json"))
            inputs = next(p for p in candidates if p.stem.removeprefix("inputs-") in prompt.name)
            records = json.loads(inputs.read_text())["records"]
            value.update(
                inputs_sha256=hashlib.sha256(inputs.read_bytes()).hexdigest(),
                ballot_refs=[r["ballot_sha256"] for r in records],
                summary="There is a tie",
                recommendation="Recommend B with caveats",
                agreements=[],
                disagreements=["Cost versus latency"],
                open_questions=[],
            )
            name = "synthesis.json"
        vote_files.atomic_json(output / name, value)
        return {"ok": True, "process_exited": True, "returncode": 0, "pid": 1073741824}

    def driver(self):
        task, attempt = self.fixture.current()
        return vote_coordinator.Coordinator(self.db, task, attempt, source_reader=self.source, launcher=self.launcher)

    def start(self):
        self.fixture.cycle([self.event()])
        return self.driver()

    def publish_all(self):
        comments = []

        def transport(command, **kwargs):
            body = next((x[5:] for x in command if x.startswith("body=")), None)
            if body is not None:
                value = {
                    "id": len(comments) + 1,
                    "body": body,
                    "user": {"login": "robert-bot"},
                }
                comments.append(value)
            elif "comments?" in command[2]:
                value = [comments]
            else:
                value = self.source()
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        result = publish.publish_ready_actions(self.db, run_command=transport)
        self.assertEqual(result["published_count"], 1, result)
        return comments

    def publication(self):
        driver = self.start()
        driver.run()
        self.fixture.cycle([])
        with vote_store.connect(self.db) as conn:
            action = dict(
                zip(
                    [d[0] for d in conn.execute("SELECT * FROM github_actions").description],
                    conn.execute("SELECT * FROM github_actions").fetchone(),
                )
            )
            result = conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
        scope = json.loads(result)["audit"]["action_scope"]
        return driver, action, scope

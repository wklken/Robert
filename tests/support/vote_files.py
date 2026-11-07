"""Shared fixtures for vote files tests."""

from pathlib import Path
import tempfile
import unittest
from robert_agent.workflows.vote import files as vote_files
from robert_agent.workflows.vote import workspace as vote_workspace


class VoteFileFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        context = self.root / "context"
        context.mkdir()
        self.manifest = {
            "default_worker": "one",
            "schema_version": 1,
            "round_id": "vote-test",
            "snapshot_id": "snapshot",
            "artifact_root": str(self.root),
            "context_dir": str(context),
            "worktree_path": str(context),
            "workers": [{"name": "one"}, {"name": "two"}],
            "sources": {"source-1": {"body": "decision"}},
            "files": {},
            "frozen": False,
            "source_type": "issue",
        }
        vote_files.atomic_json(context / "manifest.json", self.manifest)
        self.prepared = {
            "schema_version": 1,
            "round_id": "vote-test",
            "snapshot_id": "snapshot",
            "status": "ready",
            "question": "Which one?",
            "criteria": ["Reliability"],
            "options": [
                {"id": "A", "description": "A", "origin": "user"},
                {"id": "B", "description": "B", "origin": "user"},
            ],
        }
        self.manifest = vote_workspace.freeze(self.manifest, self.prepared)
        self.ballot = {
            k: self.manifest[k]
            for k in ("schema_version", "round_id", "snapshot_id", "ballot_spec_sha256")
        }
        self.ballot.update(
            choice_id="A",
            reason_summary="Public reason",
            evidence_refs=["source-1"],
            tradeoffs=[],
            uncertainties=[],
        )
        self.path = self.root / "ballot.json"

    def read(self, value):
        vote_files.atomic_json(self.path, value)
        return vote_files.read_ballot(self.path, self.manifest)

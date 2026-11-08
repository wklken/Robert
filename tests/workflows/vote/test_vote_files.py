import copy
import os
from pathlib import Path

from robert_agent.context import native_prompts
from robert_agent.context.prompts import render_prompt
from robert_agent.workflows.vote import files as vote_files
from robert_agent.workflows.vote import workspace as vote_workspace
from robert_agent.workflows.vote import coordinator as vote_coordinator
from tests.support.vote_files import VoteFileFixture


class VoteFileTests(VoteFileFixture):
    def test_stage_reading_scope_preserves_independence_and_allows_sealed_synthesis(self):
        for request in (
            native_prompts.vote_prepare_input(self.manifest),
            native_prompts.vote_cast_input(self.manifest),
        ):
            self.assertIn("read other runs", render_prompt(request).text)
        inputs = self.root / "synthesis-inputs.json"
        synthesis = render_prompt(native_prompts.vote_synthesize_input(self.manifest, inputs, "hash", [])).text
        self.assertIn(str(inputs), synthesis)
        self.assertIn("Read every sealed ballot and the program tally", synthesis)
        self.assertNotIn("read other runs", synthesis)
        self.assertIn("Read only the ballots listed in the synthesis inputs", synthesis)
        self.assertIn("Do not read unselected runs or other rounds", synthesis)

    def test_valid_vote_and_explicit_abstention(self):
        self.assertEqual(self.read(self.ballot)["choice_id"], "A")
        self.assertIsNone(self.read({**self.ballot, "choice_id": None, "abstain_reason": "Need evidence"})["choice_id"])

    def test_invalid_identity_options_missing_reasons_and_unknown_evidence(self):
        for mutation in [
            {"round_id": "old"},
            {"snapshot_id": "old"},
            {"ballot_spec_sha256": "old"},
            {"choice_id": "C"},
            {"choice_id": True},
            {"choice_id": []},
            {"reason_summary": ""},
            {"reason_summary": "x" * 1501},
            {"choice_id": None},
            {"evidence_refs": ["unknown"]},
        ]:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.read({**self.ballot, **mutation})

    def test_changed_prompt_or_spec_cannot_be_used(self):
        self.assertTrue(vote_workspace.verify_snapshot(self.manifest))
        Path(self.manifest["prompt_path"]).write_text("another question")
        with self.assertRaises(ValueError):
            vote_workspace.verify_snapshot(self.manifest)

    def test_selected_ballots_require_full_coverage_and_unchanged_hash(self):
        self.read(self.ballot)
        records = [
            {
                "worker_id": w,
                "ballot_path": str(self.path),
                "ballot_sha256": vote_files.digest(self.path),
            }
            for w in ("one", "two")
        ]
        self.assertEqual(len(vote_files.selected_ballots(self.manifest, records)), 2)
        for invalid in [records[:1], [records[0], records[0]], list(reversed(records))]:
            with self.assertRaises(ValueError):
                vote_files.selected_ballots(self.manifest, invalid)
        self.path.write_text("{}")
        with self.assertRaises(ValueError):
            vote_files.selected_ballots(self.manifest, records)

    def test_prepare_rejects_duplicate_or_missing_options(self):
        for changes in [
            {"options": []},
            {"options": [self.prepared["options"][0]] * 2},
            {"criteria": []},
        ]:
            value = {**self.prepared, **changes}
            vote_files.atomic_json(self.path, value)
            with self.assertRaises(ValueError):
                vote_files.read_prepared(self.path, self.manifest)

    def test_seal_rejects_symlink_and_fifo(self):
        driver = object.__new__(vote_coordinator.Coordinator)
        driver.manifest = self.manifest
        output = self.root / "output"
        output.mkdir()
        run = {
            "stage": "vote",
            "run_id": "vote-run-test",
            "output_dir": str(output),
            "prompt_sha256": "hash",
        }
        target = output / "ballot.json"
        target.symlink_to(self.path)
        with self.assertRaises(OSError):
            driver.sealed_output(run)
        target.unlink()
        os.mkfifo(target)
        with self.assertRaisesRegex(ValueError, "regular file"):
            driver.sealed_output(run)

    def test_freshness_ignores_new_comments_but_rejects_edits_and_deleted_sources(self):
        manifest = {
            **self.manifest,
            "sources": {
                "source-1": {"kind": "target", "title": "Decision", "body": "A/B"},
                "comment:1": {"kind": "comment", "id": 1, "body": "Compare"},
            },
        }
        current = {
            "state": "open",
            "title": "Decision",
            "body": "A/B",
            "discussion": [{"id": 1, "body": "Compare"}],
        }
        self.assertTrue(vote_workspace.same_sources(manifest, current))
        newer = copy.deepcopy(current)
        newer["discussion"].append({"id": 2, "body": "New unrelated comment"})
        self.assertTrue(vote_workspace.same_sources(manifest, newer))
        newer["discussion"][0]["body"] = "Edited decision"
        self.assertFalse(vote_workspace.same_sources(manifest, newer))
        self.assertFalse(vote_workspace.same_sources(manifest, {**current, "discussion": []}))

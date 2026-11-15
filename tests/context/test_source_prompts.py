from hashlib import sha256
import json
import unittest

from robert_agent.context.source_prompts import SourcePromptCapacity, build
from robert_agent.execution.source_model_output import validate, validate_evidence


class SourcePromptTests(unittest.TestCase):
    def setUp(self):
        self.file = {
            "repo_id": "repo",
            "path": "src/file.py",
            "revision": "sha256:" + sha256(b"actual text").hexdigest(),
            "text": "actual text",
        }
        self.reference = {name: value for name, value in self.file.items() if name != "text"}
        self.candidates = ("repo",)
        self.supplied = validate_evidence((self.file,), self.candidates)
        self.prompt = build(
            stage="identify",
            round_id="round",
            run_id="run",
            input_version="v1",
            snapshot={"ticket": "literal $tmp", "discussion": ["full detail"]},
            candidates=self.candidates,
            evidence=(self.file,),
        )

    def validate(self, data):
        return validate(
            data, stage="identify", binding=self.prompt.binding, candidates=self.candidates, supplied=self.supplied
        )

    def test_render_is_inline_and_digest_binds_body_without_reference_to_itself(self):
        outer = json.loads(self.prompt.payload)
        self.assertEqual(outer["prompt_sha256"], sha256(outer["prompt"].encode()).hexdigest())
        self.assertIn("literal $tmp", outer["prompt"])
        self.assertIn("full detail", outer["prompt"])
        self.assertIn("actual text", outer["prompt"])
        self.assertIn("tools: []", outer["prompt"])
        self.assertNotIn("file location", outer["prompt"])

    def test_json_identity_evidence_and_shape_are_exact(self):
        accepted = {
            **self.prompt.binding,
            "kind": "selection",
            "repositories": ["repo"],
            "references": [self.reference],
        }
        self.assertEqual(self.validate(json.dumps(accepted).encode()), accepted)
        for invalid in (
            {**accepted, "actions": [{"write": True}]},
            {**accepted, "input_version": "stale"},
            {**accepted, "repositories": ["other"]},
            {**accepted, "repositories": ["repo", "repo"]},
            {**accepted, "references": [{**self.reference, "path": "other.py"}]},
            {**accepted, "references": [{**self.reference, "revision": "sha256:" + "f" * 64}]},
            {**self.prompt.binding, "kind": "question", "question": "\ud800"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.validate(json.dumps(invalid).encode())
        with self.assertRaises(ValueError):
            self.validate(json.dumps(accepted).encode()[:-1] + b', "kind":"selection"}')
        with self.assertRaises(ValueError):
            self.validate(b"x" * 8193)

    def test_synthesis_can_report_insufficient_evidence(self):
        question = {**self.prompt.binding, "kind": "question", "question": "Which behavior should apply?"}
        result = validate(
            json.dumps(question).encode(),
            stage="synthesis",
            binding=self.prompt.binding,
            candidates=self.candidates,
            supplied=set(),
        )
        self.assertEqual(result, question)

    def test_required_context_capacity_is_explicit_and_file_revisions_cannot_mix(self):
        with self.assertRaises(SourcePromptCapacity):
            build(
                stage="identify",
                round_id="round",
                run_id="run",
                input_version="v1",
                snapshot={"ticket": "x" * 50000},
                candidates=self.candidates,
                evidence=(self.file,),
            )
        mixed = {**self.file, "text": "changed", "revision": "sha256:" + sha256(b"changed").hexdigest()}
        for records in ((self.file, mixed), ({**self.file, "repo_id": "foreign"},), ({**self.file, "text": "forged"},)):
            with self.subTest(records=records), self.assertRaises(ValueError):
                validate_evidence(records, self.candidates)

    def test_new_terminal_output_declares_exact_whole_frozen_members(self):
        members = [
            {
                "request_id": "req1",
                "content_version": "version",
                "events": [
                    {"row_id": 8, "event_id": "event-id", "fingerprint": "stored-fingerprint"},
                    {"row_id": 9, "event_id": "other-id", "fingerprint": "second-fingerprint"},
                ],
            }
        ]
        base = {**self.prompt.binding, "kind": "question", "question": "Which component?"}
        accepted = {**base, "handled_event_fingerprints": ["stored-fingerprint", "second-fingerprint"]}
        settings = dict(
            stage="identify",
            binding=self.prompt.binding,
            candidates=self.candidates,
            supplied=self.supplied,
            members=members,
        )
        self.assertEqual(validate(json.dumps(accepted).encode(), **settings), accepted)
        for candidate in (
            base,
            {**accepted, "handled_event_fingerprints": ["stored-fingerprint"]},
            {**accepted, "handled_event_fingerprints": ["event-id", "other-id"]},
        ):
            with self.assertRaises(ValueError):
                validate(json.dumps(candidate).encode(), **settings)

"""Final required gates retain honest historical verification evidence."""

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from robert_agent.delivery.contract import InvalidSubmission
from robert_agent.workflows.pre_pr_review import coordinator
from robert_agent.workflows.pre_pr_review.verification import final_verification_checks
from tests.support.review_workspace import ReviewWorkspaceFixture


def check(*, required=True, status="passed", purpose="Verify the final repaired source.py."):
    return {
        "command": ["python3", "-B", "-m", "unittest", "tests.test_source"],
        "status": status,
        "purpose": purpose,
        "required": required,
        "exit_code": 0 if status == "passed" else 1,
        "source": "worker_reported",
    }


class FinalVerificationTests(ReviewWorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.output = self.root / "address-output"
        self.output.mkdir()
        self.report = self.root / "report.md"
        self.report.write_text("F1: retain honest regression evidence.\n")
        self.report_sha = coordinator.store.digest(self.report)
        self.candidate = self.root / "candidate.json"
        self.candidate.write_text(
            json.dumps(
                {
                    "planned_github_actions": [
                        {"type": "push_existing_pr"},
                        {"type": "open_pr", "title": "Fix verification", "body": "Verified the final source."},
                    ],
                    "used_skills": [],
                    "consumed_event_fingerprints": ["comment:1"],
                    "verification": [check()],
                }
            )
        )
        self.settings = {
            "origin_type": "github",
            "task_kind": "human_request",
            "allowed_github_actions": ["push_existing_pr", "open_pr", "comment"],
            "expected_output": "new_pr",
            "verification_policy": {"mode": "required", "required_statuses": ["passed"], "allow_skipped": False},
            "action_scope": {
                "repo": "x/y",
                "workstream_id": "github:x/y#42",
                "worktree_path": str(self.repo),
                "branch_name": "feature",
                "base_branch": "main",
                "remote": "upstream",
                "sources": [],
            },
        }
        self.driver = SimpleNamespace(
            db_path="fixture-unused.sqlite3",
            task_id="task-1",
            attempt_id="attempt-1",
            manifest={
                "head_sha": self.head,
                "candidate": {
                    "candidate_path": str(self.candidate),
                    "candidate_sha256": coordinator.store.digest(self.candidate),
                },
            },
            request={},
            original_request=None,
            final_feedback=None,
            handoff_source_run_id=None,
            implementer={"name": "writer"},
            phase=Mock(),
            address_workspace=Mock(return_value=self.repo),
            prompt=self.prompt,
            run_phase=self.run_phase,
        )
        for name in ("candidate_submission", "validate_submission", "envelope"):
            setattr(self.driver, name, MethodType(getattr(coordinator.Coordinator, name), self.driver))
        self.response = {
            "candidate_sha": self.head,
            "final_sha": self.head,
            "report_sha256": self.report_sha,
            "body": "Verified the final source and retained the historical failures.",
            "dispositions": [{"id": "F1", "status": "dismissed", "reason": "Historical negative control."}],
            "verification": [check()],
        }

    def prompt(self, name, text, **kwargs):
        path = self.root / name
        path.write_text(text)
        return path

    def run_phase(self, stage, worker, prompt, validate, **kwargs):
        self.assertEqual(stage, "address_findings")
        path = self.output / "dispositions.json"
        path.write_text(json.dumps(self.response))
        return validate(self.output, "fixture-model")

    def address(self):
        # These are the registered attempt facts, not a replacement contract or
        # audit. The actual coordinator composes both original validators.
        with (
            patch.object(coordinator.store, "connect", return_value=nullcontext(None)),
            patch.object(coordinator.validation, "context", return_value=(self.settings, None)),
        ):
            return coordinator.Coordinator.address(self.driver, self.report, self.report_sha, ["F1"])

    def validate_original_delivery(self, checks):
        draft = self.driver.candidate_submission()
        draft["verification"] = checks
        with (
            patch.object(coordinator.store, "connect", return_value=nullcontext(None)),
            patch.object(coordinator.validation, "context", return_value=(self.settings, None)),
        ):
            return self.driver.validate_submission(draft)

    def test_historical_optional_red_and_mutation_failures_preserve_evidence(self):
        historical = [
            check(
                required=False,
                status="failed",
                purpose=f"Historical RED before repair at {self.base}: expected the reported regression to fail.",
            ),
            check(
                required=False,
                status="failed",
                purpose=f"Mutation negative control at {self.head}: removing the guard must fail test_source.",
            ),
        ]
        historical[1]["command"] = "python3 mutation_check.py --remove-guard"
        self.response["verification"] = [*historical, check()]
        before = deepcopy(self.response)

        artifact = self.address()

        self.assertEqual(artifact["head"], self.head)
        self.assertEqual(json.loads(Path(artifact["dispositions_path"]).read_text()), before)
        self.assertEqual(self.response, before)

    def test_required_failure_remains_blocked_alongside_required_success(self):
        self.response["verification"] = [check(status="failed"), check()]
        before = deepcopy(self.response)
        with self.assertRaises(ValueError):
            self.address()
        with self.assertRaisesRegex(
            ValueError, "verification policy failed: route requires every required verification"
        ):
            self.validate_original_delivery(self.response["verification"])
        self.assertEqual(self.response, before)

    def test_only_optional_checks_cannot_satisfy_final_required_gates(self):
        for checks in ([], [check(required=False)], [check(required=False, status="failed")]):
            with self.subTest(checks=checks):
                self.response["verification"] = checks
                with self.assertRaises(ValueError):
                    self.address()
                with self.assertRaisesRegex(
                    ValueError, "verification policy failed: route requires every required verification"
                ):
                    self.validate_original_delivery(checks)

    def test_required_skipped_check_cannot_replace_final_execution(self):
        skipped = check(status="skipped")
        skipped.pop("exit_code")
        skipped["skipped_reason"] = "Required tests were not run."
        self.response["verification"] = [skipped, check()]
        with self.assertRaises(ValueError):
            self.address()
        with self.assertRaisesRegex(ValueError, "verification policy failed: required verification cannot be skipped"):
            self.validate_original_delivery(self.response["verification"])

    def test_original_contract_refuses_malformed_optional_evidence(self):
        cases = [
            ({"exit_code": False}, "exit_code"),
            ({"exit_code": "1"}, "exit_code"),
            ({"exit_code": 0}, "exit_code"),
            ({"status": "passed", "exit_code": 1}, "exit_code"),
            ({"status": "unknown"}, "status"),
            ({"status": []}, "status"),
            ({"source": "invented"}, "source"),
            ({"source": []}, "source"),
            ({"source": "runtime_recorded"}, "command_record_path"),
            ({"source": "runtime_recorded", "command_record_path": ""}, "command_record_path"),
            ({"source": "runtime_recorded", "command_record_path": []}, "command_record_path"),
            ({"source": "external_ci"}, "url"),
            ({"source": "external_ci", "url": "http://ci.invalid/run/1"}, "url"),
            ({"source": "external_ci", "url": []}, "url"),
            ({"required": "false"}, "required"),
            ({"command": []}, "command"),
            ({"purpose": " "}, "purpose"),
            ({"source_commit": self.head}, None),
        ]
        for mutation, field in cases:
            with self.subTest(mutation=mutation):
                evidence = check(required=False, status="failed") | mutation
                self.response["verification"] = [evidence, check()]
                before = deepcopy(self.response)
                with self.assertRaises(InvalidSubmission) as raised:
                    self.address()
                self.assertEqual(raised.exception.field, "verification[0]" + (f".{field}" if field else ""))
                self.assertEqual(self.response, before)

    def test_original_contract_refuses_missing_fields_and_fabricated_skips(self):
        for field in ("command", "status", "purpose", "required", "source", "exit_code"):
            with self.subTest(field=field):
                evidence = check(required=False)
                evidence.pop(field)
                with self.assertRaises(InvalidSubmission) as raised:
                    self.validate_original_delivery([evidence, check()])
                self.assertEqual(raised.exception.field, f"verification[0].{field}")
        for evidence, field in (
            (check(required=False, status="skipped") | {"skipped_reason": "Not executed."}, "exit_code"),
            (
                {key: value for key, value in check(required=False, status="skipped").items() if key != "exit_code"},
                "skipped_reason",
            ),
        ):
            with self.subTest(evidence=evidence):
                with self.assertRaises(InvalidSubmission) as raised:
                    self.validate_original_delivery([evidence, check()])
                self.assertEqual(raised.exception.field, f"verification[0].{field}")

    def test_valid_historical_ci_and_runtime_descriptors_are_preserved(self):
        self.response["verification"] = [
            check(required=False, status="failed") | {"source": "external_ci", "url": "https://ci.invalid/run/42"},
            check(required=False, status="failed")
            | {"source": "runtime_recorded", "command_record_path": "/recorded/history/command.json"},
            check(),
        ]
        before = deepcopy(self.response)
        artifact = self.address()
        self.assertEqual(json.loads(Path(artifact["dispositions_path"]).read_text()), before)

    def test_final_sha_must_match_the_clean_writer_commit(self):
        for sha in (self.base, "a" * 40, self.head[:12], None):
            with self.subTest(sha=sha):
                self.response["final_sha"] = sha
                with self.assertRaisesRegex(ValueError, "final worktree HEAD"):
                    self.address()

    def test_dirty_final_writer_cannot_claim_passed_verification(self):
        (self.repo / "source.py").write_text("value = 99\n")
        with self.assertRaises(ValueError):
            self.address()


class FinalVerificationPolicyTests(unittest.TestCase):
    def test_policy_returns_the_original_list_with_opaque_optional_entries(self):
        checks = [check(required=False, status="failed"), check()]
        before = deepcopy(checks)
        self.assertIs(final_verification_checks(checks), checks)
        self.assertEqual(checks, before)

    def test_optional_failure_is_not_inferred_to_be_historical_from_its_flag(self):
        checks = [
            check(required=False, status="failed", purpose="Investigate an unresolved optional failure."),
            check(),
        ]
        before = deepcopy(checks)
        self.assertIs(final_verification_checks(checks), checks)
        self.assertEqual(checks, before)

    def test_policy_requires_every_declared_required_gate_to_pass(self):
        for checks in ([], [check(required=False)], [check(status="failed"), check()], [check(status="skipped")]):
            with self.subTest(checks=checks):
                before = deepcopy(checks)
                with self.assertRaises(ValueError):
                    final_verification_checks(checks)
                self.assertEqual(checks, before)

    def test_policy_does_not_replace_the_original_schema_validator(self):
        checks = [check() | {"exit_code": "0"}]
        self.assertIs(final_verification_checks(checks), checks)

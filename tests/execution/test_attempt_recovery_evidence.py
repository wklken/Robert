"""Deadline attention needs a durable candidate and proven child exit."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from robert_agent.execution import operations, ownership, rounds
from robert_agent.execution.application import evidence
from robert_agent.persistence import execution_control
from robert_agent.persistence.rounds import pre_pr_review
from robert_agent.persistence.schema import database
from tests.support import database as fixture_database
from tests.support.temp_workspace import TemporaryWorkspace


class AttemptRecoveryEvidenceTests(TemporaryWorkspace):
    def setUp(self):
        super().setUp()
        self.db = self.root / "recovery.sqlite3"
        database.init_database(self.db)
        self.now = datetime.now(timezone.utc)
        timestamp = self.now.isoformat()
        self.deadline = (self.now - timedelta(seconds=60)).isoformat()
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("Implement the original request.")
        self.candidate = self.root / "candidate.json"
        self.candidate.write_text('{"handoff": "Completed implementation"}')
        self.exited = {"ok": True, "pid": 1073741824, "process_exited": True, "returncode": 0}
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES('repo','x/y','bot','main','/repo','/trees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) "
                "VALUES('stream','repo','active','task',?,?)",
                (timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                "VALUES('task','stream','running',?,?)",
                (timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) "
                "VALUES('attempt','task',1,'failed',?)",
                (json.dumps({"dispatch": self.exited}),),
            )
            self.assertTrue(ownership.claim_task(conn, "task", timestamp))
            pre_pr_review.create(
                conn,
                "task",
                {
                    "prompt_path": str(self.prompt),
                    "prompt_sha256": pre_pr_review.digest(self.prompt),
                },
            )
            entry = pre_pr_review.session(conn, task_id="task")
            self.round_id = entry["round_id"]
            run = pre_pr_review.new_run(conn, self.round_id, "implement", "coder", "attempt", self.root, "prompt")
            self.run_id = run["run_id"]
            self.artifact = {
                "candidate_path": str(self.candidate),
                "candidate_sha256": pre_pr_review.digest(self.candidate),
                "head": "a" * 40,
            }
            pre_pr_review.update_run(
                conn,
                self.run_id,
                status="succeeded",
                slot_reserved=0,
                dispatch_json=json.dumps(self.exited),
                artifact_json=json.dumps(self.artifact),
            )
            pre_pr_review.save_snapshot(
                conn,
                self.round_id,
                {
                    "snapshot_id": "snapshot",
                    "head_sha": "a" * 40,
                    "base_sha": "b" * 40,
                    "candidate": self.artifact,
                },
            )
            pre_pr_review.update(
                conn,
                self.round_id,
                status="attention",
                deadline_at=self.deadline,
                error="review: pre-publication review deadline expired",
            )

    def inspect(self):
        with fixture_database.connect(self.db) as conn:
            before = conn.total_changes
            result = evidence.inspect_attempt_recovery(conn, "attempt", "task", self.root)
            self.assertEqual(conn.total_changes, before)
            return result

    def test_expired_deadline_with_verified_candidate_and_released_children_is_recoverable(self):
        with patch.object(rounds, "terminate_children", side_effect=AssertionError("inspection must not signal")):
            result = self.inspect()
        self.assertEqual(result["status"], "recoverable_progress")
        self.assertEqual(result["reason_code"], "pre_pr_review_deadline")
        self.assertEqual(result["round_id"], self.round_id)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(pre_pr_review.session(conn, task_id="task")["deadline_at"], self.deadline)
            self.assertEqual(execution_control.get_binding(conn, "task", "task")["recovery_attempts"], 0)

    def test_deadline_does_not_exempt_canceled_superseded_or_unrelated_attention(self):
        for status, error in (
            ("canceled", "review: pre-publication review deadline expired"),
            ("superseded", "review: pre-publication review deadline expired"),
            ("attention", "review: worker identity requires attention"),
            ("attention", "review: unsafe secret; pre-publication review deadline expired"),
            ("attention", "review: pre-publication review deadline expired; another failure"),
        ):
            with self.subTest(status=status, error=error):
                with fixture_database.connect(self.db) as conn:
                    pre_pr_review.update(conn, self.round_id, status=status, error=error)
                self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_refreshed_candidate_head_keeps_immutable_implementation_checkpoint(self):
        with fixture_database.connect(self.db) as conn:
            entry = pre_pr_review.session(conn, task_id="task")
            manifest = entry["manifest"]
            manifest["head_sha"] = "c" * 40
            manifest["candidate"] = {**self.artifact, "head": "c" * 40}
            pre_pr_review.save_snapshot(conn, self.round_id, manifest)
        self.assertEqual(self.inspect()["status"], "recoverable_progress")
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(pre_pr_review.runs(conn, self.round_id)[0]["artifact"], self.artifact)

    def test_candidate_head_must_match_current_snapshot_head(self):
        with fixture_database.connect(self.db) as conn:
            manifest = pre_pr_review.session(conn, task_id="task")["manifest"]
            manifest["head_sha"] = "c" * 40
            pre_pr_review.save_snapshot(conn, self.round_id, manifest)
        self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_attention_requires_real_expired_deadline(self):
        for deadline in (None, "invalid", (self.now + timedelta(hours=1)).isoformat()):
            with self.subTest(deadline=deadline):
                with fixture_database.connect(self.db) as conn:
                    pre_pr_review.update(conn, self.round_id, deadline_at=deadline)
                self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_candidate_and_original_prompt_hashes_must_still_match(self):
        for path in (self.candidate, self.prompt):
            original = path.read_bytes()
            for missing in (False, True):
                with self.subTest(file=path.name, missing=missing):
                    if missing:
                        path.unlink()
                    else:
                        path.write_text("changed checkpoint")
                    self.assertEqual(self.inspect()["status"], "unrecoverable")
                    path.write_bytes(original)

    def test_unregistered_or_unsuccessful_candidate_is_not_progress(self):
        for changes in (
            {"status": "failed"},
            {"artifact_json": "{}"},
            {"artifact_json": json.dumps({**self.artifact, "candidate_path": str(self.root / "other.json")})},
            {"artifact_json": json.dumps({**self.artifact, "candidate_sha256": "0" * 64})},
        ):
            with self.subTest(changes=changes):
                with fixture_database.connect(self.db) as conn:
                    pre_pr_review.update_run(conn, self.run_id, **changes)
                self.assertEqual(self.inspect()["status"], "unrecoverable")
                with fixture_database.connect(self.db) as conn:
                    pre_pr_review.update_run(
                        conn, self.run_id, status="succeeded", artifact_json=json.dumps(self.artifact)
                    )
        with fixture_database.connect(self.db) as conn:
            conn.execute("DELETE FROM pre_pr_review_snapshots")
        self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_terminal_child_status_and_released_slot_do_not_prove_process_exit(self):
        for status, reserved, dispatch in (
            ("succeeded", 1, self.exited),
            ("succeeded", 0, {"pid": 1073741824}),
            ("succeeded", 0, {}),
            ("succeeded", 0, None),
            ("succeeded", 0, []),
            ("succeeded", 0, "malformed-dispatch"),
            ("running", 0, {}),
            ("succeeded", 0, {**self.exited, "cleanup_pending": True}),
        ):
            with self.subTest(status=status, reserved=reserved, dispatch=dispatch):
                with fixture_database.connect(self.db) as conn:
                    pre_pr_review.update_run(
                        conn, self.run_id, status=status, slot_reserved=reserved, dispatch_json=json.dumps(dispatch)
                    )
                self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_new_or_changed_child_observation_cannot_authorize_recovery(self):
        with patch.object(rounds, "exit_observation_current", return_value=False):
            self.assertEqual(self.inspect()["status"], "unrecoverable")
        with fixture_database.connect(self.db) as conn:
            run = pre_pr_review.new_run(conn, self.round_id, "review", "reviewer", "attempt", self.root, "prompt")
            pre_pr_review.update_run(conn, run["run_id"], status="running", slot_reserved=1)
        self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_submission_rejection_and_non_pre_pr_attention_are_not_deadline_recovery(self):
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.submission_validation',json(?))",
                (json.dumps({"reason_code": "redaction_blocked"}),),
            )
        self.assertEqual(self.inspect()["status"], "unrecoverable")
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE attempts SET metadata_json=?", (json.dumps({"dispatch": self.exited}),))
            entry = pre_pr_review.session(conn, task_id="task")
        with patch.object(rounds, "session", return_value=entry):
            self.assertEqual(self.inspect()["status"], "unrecoverable")

    def test_deadline_recovery_still_consumes_only_existing_one_shot_quota(self):
        result = self.inspect()
        with fixture_database.connect(self.db) as conn:
            for expected in ("recover_once", "attention"):
                self.assertEqual(
                    operations.classify_recovery(
                        conn, "task", "task", "attempt", {"identity_status": "dead", **result}, now=self.now.isoformat()
                    ),
                    expected,
                )
            self.assertEqual(execution_control.get_binding(conn, "task", "task")["recovery_attempts"], 1)

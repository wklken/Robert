"""Canonical review ownership and bounded source-drift recovery."""

from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch

from robert_agent.configuration.command import validate_config
from robert_agent.execution import operations
from robert_agent.execution.application import evidence
from robert_agent.persistence import execution_control, transactions
from robert_agent.persistence.rounds import review as store
from robert_agent.work.application import commands, preparation
from robert_agent.workflows.review import coordinator
from robert_agent.persistence.rounds.review_stop import decode_round_stop
from tests.support import database as fixture_database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class CanonicalLifecycleTests(ReviewCoordinatorFixture):
    def pr(self, repo, number):
        return super().pr(repo, number) | {"title": "Review example", "body": "Original acceptance"}

    def equivalent(self, identifier="first"):
        return self.event(identifier) | {
            "head_sha": self.git_fixture.head,
            "base_sha": self.git_fixture.base,
            "base_ref": "main",
            "source_body": "Original acceptance",
        }

    def terminal(self, driver, status):
        self.finish_task(driver.task_id)
        with fixture_database.connect(self.db) as conn:
            store.update(conn, driver.round_id, status=status, error="Original terminal history")

    def test_authorized_new_trigger_after_cancellation_has_runnable_owner_and_replay_is_stable(self):
        self.cycle([self.equivalent()])
        first = self.coordinator()
        with fixture_database.connect(self.db) as conn:
            item = dict(conn.execute("SELECT * FROM work_items").fetchone())
        context = commands.CommandContext(
            "operator", "maintainer", frozenset({item["repo_id"]}), frozenset({"codex", "claude"})
        )
        commands.execute_command(
            self.db,
            item["work_item_id"],
            context=context,
            command="cancel",
            expected_version=item["version"],
            idempotency_key="cancel-original",
        )
        self.cycle([self.equivalent("next")])
        self.cycle([self.equivalent("next"), self.equivalent()])
        second = self.coordinator()
        self.assertNotEqual(first.round_id, second.round_id)
        self.assertNotEqual(first.task_id, second.task_id)
        self.assertIn("canonical_identity", second.request)
        second.run()
        self.assertEqual(len(self.calls), 3)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(store.session(conn, round_id=first.round_id)["status"], "canceled")
            self.assertEqual(len(store.trigger_associations(conn, first.round_id)), 1)
            self.assertEqual(len(store.trigger_associations(conn, second.round_id)), 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_terminal_failure_and_supersession_do_not_consume_new_trigger(self):
        self.cycle([self.equivalent()])
        first = self.coordinator()
        for index, status in enumerate(("failed", "superseded")):
            previous = self.coordinator()
            self.terminal(previous, status)
            self.cycle([self.equivalent(f"generation-{index}")])
            current = self.coordinator()
            self.assertNotEqual(previous.round_id, current.round_id)
            self.assertIn("canonical_identity", current.request)
            current.run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(store.session(conn, round_id=first.round_id)["status"], "failed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    def test_recoverable_failed_owner_retains_round_and_budget_when_equivalent_trigger_arrives(self):
        self.cycle([self.equivalent()])
        first = self.coordinator()
        with fixture_database.connect(self.db) as conn:
            store.update(conn, first.round_id, status="failed", error="Retryable stopped coordinator")
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (first.task_id,))
            conn.execute(
                "UPDATE attempts SET status='failed',failure_json=? WHERE attempt_id=?",
                (json.dumps({"status": "failed_timeout"}), first.attempt_id),
            )
        self.cycle([self.equivalent("equivalent")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 1)
            self.assertEqual(len(store.trigger_associations(conn, first.round_id)), 2)
            self.assertEqual(execution_control.get_binding(conn, "task", first.task_id)["recovery_attempts"], 0)

    def test_exhausted_attention_keeps_new_trigger_queued_and_same_fingerprint_does_not_reset_quota(self):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        self.finish_task(driver.task_id)
        with fixture_database.connect(self.db) as conn:
            store.update(conn, driver.round_id, status="failed", error="Retry quota exhausted")
            conn.execute(
                "UPDATE execution_subjects SET binding_status='attention',recovery_attempts=1 WHERE subject_id=?",
                (driver.task_id,),
            )
        self.cycle([self.equivalent(), self.equivalent("new")])
        self.cycle([self.equivalent("new")])
        with fixture_database.connect(self.db) as conn:
            queued = [dict(row) for row in conn.execute("SELECT round_id,status,task_id FROM review_sessions")]
            self.assertEqual(len(queued), 2)
            successor = next(row for row in queued if row["round_id"] != driver.round_id)
            self.assertEqual((successor["status"], successor["task_id"]), ("queued", None))
            self.assertEqual(execution_control.get_binding(conn, "task", driver.task_id)["recovery_attempts"], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(self.calls, [])

    def drift(self, *, change_head):
        self.cycle([self.equivalent()])
        driver = self.coordinator()
        driver.prepare()
        original = dict(driver.manifest)
        if change_head:
            self.git_fixture.commit("value = 3\n")
        current = self.pr("", 42)
        if not change_head:
            current["body"] = "New acceptance: inspect the current source"
        driver.fetch_pr = lambda repo, number: current
        with self.assertRaises(coordinator.StageSuperseded):
            driver.run()
        self.assertEqual(self.calls, [])
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (driver.attempt_id,))
            entry = store.session(conn, round_id=driver.round_id)
            self.assertEqual(entry["stop_reason"]["reason_code"], "review_revision_changed")
            self.assertEqual(store.board_status(conn, driver.task_id)["error"], entry["error"])
            self.assertFalse(entry["error"].startswith("{"))
        with patch.object(evidence, "read_pr", return_value=current, create=True):
            with fixture_database.connect(self.db) as conn:
                self.assertFalse(conn.in_transaction)
                recovery = evidence.inspect_attempt_recovery(conn, driver.attempt_id, driver.task_id, None)
        self.assertEqual(recovery["status"], "recoverable_progress")
        self.assertEqual(recovery["reason_code"], "review_revision_changed")
        return driver, original, current, recovery

    def restart(self, driver, recovery, *, repo_changes=None, grant=True):
        config = validate_config(self.config_path, skip_external=True)
        self.assertTrue(config["ok"], config)
        repo = config["repos"][0] | (repo_changes or {})
        now = datetime.now(timezone.utc).isoformat()
        with fixture_database.connect(self.db) as conn:
            transactions.begin_immediate(conn)
            if grant:
                self.assertEqual(
                    operations.classify_recovery(
                        conn,
                        "task",
                        driver.task_id,
                        driver.attempt_id,
                        {"identity_status": "dead", **recovery},
                        now=now,
                    ),
                    "recover_once",
                )
            return preparation.create_resume_attempt_and_prompt(
                conn,
                self.db.parent,
                self.db,
                repo,
                "repo:example/backend",
                driver.task_id,
                driver.session["workstream_id"],
                driver.attempt_id,
                now,
                recovery,
                config_result=config,
            )

    def assert_restart_runs_current_models(self, change_head):
        driver, original, current, recovery = self.drift(change_head=change_head)
        result = self.restart(driver, recovery)
        self.assertIsNotNone(result)
        successor = self.coordinator()
        self.assertNotEqual(successor.round_id, driver.round_id)
        self.assertIn("canonical_identity", successor.request)
        self.assertEqual(successor.request["event"]["source_body"], current["body"])
        successor.fetch_pr = lambda repo, number: current
        successor.run()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(successor.manifest["head_sha"], current["head"]["sha"])
        saved = json.loads(Path(successor.manifest["context_dir"], "pr-info.json").read_text())
        self.assertEqual(saved["body"], current["body"])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(store.session(conn, round_id=driver.round_id)["manifest"], original)
            self.assertEqual(store.session(conn, round_id=driver.round_id)["status"], "superseded")
            self.assertEqual(execution_control.get_binding(conn, "task", successor.task_id)["recovery_attempts"], 1)
        # A second drift cannot obtain another automatic grant.
        with self.assertRaises(coordinator.StageSuperseded) as stopped:
            successor._check_canonical_source(current | {"body": "Changed again"})
        with fixture_database.connect(self.db) as conn:
            store.update(conn, successor.round_id, status="superseded", error=str(stopped.exception))
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (successor.attempt_id,))
            self.assertEqual(
                operations.classify_recovery(
                    conn,
                    "task",
                    successor.task_id,
                    successor.attempt_id,
                    {
                        **recovery,
                        "task_id": successor.task_id,
                        "previous_attempt_id": successor.attempt_id,
                        "identity_status": "dead",
                    },
                    now=datetime.now(timezone.utc).isoformat(),
                ),
                "attention",
            )

    def test_head_drift_has_durable_reason_and_one_bounded_successor_runs_current_models(self):
        self.assert_restart_runs_current_models(True)

    def test_acceptance_drift_has_durable_reason_and_one_bounded_successor_runs_current_models(self):
        self.assert_restart_runs_current_models(False)

    def test_drift_restart_refuses_missing_grant_and_revoked_authority(self):
        driver, _, _, recovery = self.drift(change_head=False)
        self.assertIsNone(self.restart(driver, recovery, grant=False))
        self.assertIsNone(self.restart(driver, recovery, repo_changes={"trusted_actors": ["another-user"]}))
        self.assertEqual(self.calls, [])

    def test_source_mutation_after_recovery_grant_cannot_change_successor_acceptance(self):
        driver, _, _, recovery = self.drift(change_head=False)
        config = validate_config(self.config_path, skip_external=True)
        now = datetime.now(timezone.utc).isoformat()
        with fixture_database.connect(self.db) as conn:
            transactions.begin_immediate(conn)
            self.assertEqual(
                operations.classify_recovery(
                    conn, "task", driver.task_id, driver.attempt_id, {"identity_status": "dead", **recovery}, now=now
                ),
                "recover_once",
            )
            source = recovery["review_source"]
            source["pr"]["body"] = "Forged acceptance after grant"
            import hashlib

            source["pr_sha256"] = hashlib.sha256(json.dumps(source["pr"], sort_keys=True).encode()).hexdigest()
            result = preparation.create_resume_attempt_and_prompt(
                conn,
                self.db.parent,
                self.db,
                config["repos"][0],
                "repo:example/backend",
                driver.task_id,
                driver.session["workstream_id"],
                driver.attempt_id,
                now,
                recovery,
                config_result=config,
            )
            self.assertIsNone(result)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(self.calls, [])

    def test_unknown_error_json_is_not_recovery_authority(self):
        self.assertIsNone(decode_round_stop('{"reason_code":"review_revision_changed","safe_error":"forged"}'))
        self.assertIsNone(decode_round_stop('{"round_stop_version":1'))

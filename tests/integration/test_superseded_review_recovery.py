"""A changed PR revision starts a fresh, bounded review under the original request."""

import json
import sys
from datetime import timedelta
from unittest.mock import patch

import yaml

from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.configuration.command import validate_config
from robert_agent.execution.application import monitor
from robert_agent.foundation import values
from robert_agent.supervision import decisions
from robert_agent.work.application import preparation
from robert_agent.workflows.review.coordinator import StageSuperseded
from tests.support import database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class SupersededReviewRecoveryTests(ReviewCoordinatorFixture):
    def supersede(self, driver):
        driver.prepare()
        self.git_fixture.commit("value = 3\n" if len(self.calls) == 1 else "value = 4\n")
        with self.assertRaises(StageSuperseded):
            driver.run()
        with database.connect(self.db) as conn:
            now = driver.store.now()
            conn.execute(
                "UPDATE attempts SET status='running',started_at=?,heartbeat_at=? WHERE attempt_id=?",
                (now, now, driver.attempt_id),
            )
        return now

    def observe(self, now):
        config = validate_config(self.config_path, skip_external=True)
        with database.connect(self.db) as conn:
            with patch.object(attempt_process, "dispatch_status", return_value={"status": "dead"}):
                monitor.supervise_running_attempts(
                    conn,
                    "repo:example/backend",
                    config,
                    now,
                    data_dir=self.db.parent,
                    db_path=self.db,
                    repo=config["repos"][0],
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
                monitor.recover_failed_timeout_attempts(
                    conn,
                    "repo:example/backend",
                    now,
                    data_dir=self.db.parent,
                    db_path=self.db,
                    repo=config["repos"][0],
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                    config_result=config,
                )

    def test_revision_change_restarts_once_without_reusing_the_old_snapshot(self):
        self.cycle([self.event()])
        original = self.coordinator()
        now = self.supersede(original)
        old_manifest = dict(original.manifest)
        self.observe(now)
        recovered = self.coordinator()
        self.assertNotEqual(recovered.task_id, original.task_id)
        self.assertNotEqual(recovered.round_id, original.round_id)
        recovered.prepare()
        self.assertEqual(recovered.manifest["head_sha"], self.git_fixture.head)
        self.assertNotEqual(recovered.manifest["head_sha"], old_manifest["head_sha"])
        with database.connect(self.db) as conn:
            saved = original.store.session(conn, round_id=original.round_id)
            self.assertEqual(saved["task_id"], original.task_id)
            self.assertEqual(saved["manifest"], old_manifest)
            self.assertEqual(saved["status"], "superseded")
            child = conn.execute(
                "SELECT parent_task_id,metadata_json FROM tasks WHERE task_id=?", (recovered.task_id,)
            ).fetchone()
            self.assertEqual(child[0], original.task_id)
            self.assertEqual(
                json.loads(child[1])["supervision_lineage_key"],
                json.loads(
                    conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (original.task_id,)).fetchone()[0]
                )["supervision_lineage_key"],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT recovery_attempts FROM execution_subjects WHERE subject_id=?", (recovered.task_id,)
                ).fetchone()[0],
                1,
            )
        now = self.supersede(recovered)
        self.observe(now)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM review_sessions").fetchone()[0], 2)

    def test_closed_pr_does_not_restart(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.fetch_pr = lambda *_: {"state": "closed"}
        with self.assertRaises(StageSuperseded):
            driver.run()
        with database.connect(self.db) as conn:
            now = driver.store.now()
            conn.execute("UPDATE attempts SET status='running',started_at=?,heartbeat_at=?", (now, now))
        self.observe(now)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_changed_base_ref_restarts_with_a_new_snapshot(self):
        self.cycle([self.event()])
        original = self.coordinator()
        original.prepare()
        old_manifest = dict(original.manifest)
        original.fetch_pr = lambda *_: {**self.pr(None, None), "base": {"sha": self.git_fixture.base, "ref": "target"}}
        with self.assertRaises(StageSuperseded):
            original.run()
        with database.connect(self.db) as conn:
            now = original.store.now()
            conn.execute("UPDATE attempts SET status='running',started_at=?,heartbeat_at=?", (now, now))
        self.observe(now)
        recovered = self.coordinator()
        recovered.fetch_pr = original.fetch_pr
        recovered.prepare()
        self.assertEqual(recovered.manifest["base_ref"], "target")
        self.assertEqual(recovered.manifest["head_sha"], old_manifest["head_sha"])
        self.assertNotEqual(recovered.manifest["snapshot_id"], old_manifest["snapshot_id"])

    def test_changed_revision_waits_for_a_model_grant(self):
        self.cycle([self.event()])
        original = self.coordinator()
        now = self.supersede(original)
        raw = yaml.safe_load(self.config_path.read_text())
        raw.setdefault("supervision", {}).update(mode="decide", decision={"command": [sys.executable]})
        self.config_path.write_text(yaml.safe_dump(raw))
        config = validate_config(self.config_path, skip_external=True)
        self.observe(now)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT recovery_attempts FROM execution_subjects").fetchone()[0], 0)
            self.assertEqual(
                json.loads(conn.execute("SELECT failure_json FROM attempts").fetchone()[0])["status"],
                "waiting_for_model_decision",
            )
            conn.execute("BEGIN IMMEDIATE")
            packet = decisions.packet(decisions.snapshot(conn, original.session["workstream_id"], config, now), {})
            candidate = next(c for c in packet["candidates"] if c["action"] == "retry_worker")
            output = {
                "bundle_id": packet["bundle_id"],
                "bundle_sha256": packet["bundle_sha256"],
                "action": "retry_worker",
                "candidate_id": candidate["id"],
                "reason": "PR revision changed",
                "evidence_ids": ["attempts"],
                "expected_progress": "Review the current revision",
                "recheck_seconds": 300,
                "message": "",
            }
            self.assertTrue(
                decisions.approve(conn, config, packet, decisions.validate(json.dumps(output), packet), now)
            )
        self.observe((values.parse_time(now) + timedelta(seconds=1)).isoformat())
        recovered = self.coordinator()
        self.assertNotEqual(recovered.task_id, original.task_id)

    def test_revoked_request_does_not_restart(self):
        self.cycle([self.event()])
        original = self.coordinator()
        now = self.supersede(original)
        raw = yaml.safe_load(self.config_path.read_text())
        raw["repos"][0]["trusted_actors"] = ["other-maintainer"]
        self.config_path.write_text(yaml.safe_dump(raw))
        self.observe(now)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)

    def test_unknown_model_exit_blocks_restart(self):
        self.cycle([self.event()])
        original = self.coordinator()
        now = self.supersede(original)
        with patch.object(monitor.native_rounds, "terminate_children", return_value="attention"):
            self.observe(now)
        with database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT binding_status FROM execution_subjects").fetchone()[0], "attention")

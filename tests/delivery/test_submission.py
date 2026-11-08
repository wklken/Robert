from tests.support.results import internal_result
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch
from tests.support.worker_protocol import WorkerProtocolFixture


class SubmissionTests(WorkerProtocolFixture):
    def test_classification_fallback_survives_spool_replay_and_duplicate_submission(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.delivery import contract as result_contract
        from robert_agent.execution import spool as result_spool

        db_path = self._init_attempt_db(output_type="classification_result", route_id="classification-result")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="classification_result",
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="The requested outcome is unclear.",
            recommended_route="",
        )
        with patch.object(result, "_record_result_db", side_effect=sqlite3.OperationalError("database is locked")):
            record = result.record_result(db_path, payload)
        self.assertEqual(record["status"], "deferred", record)
        pending = list(result_spool.directory(db_path).glob("*.json"))
        self.assertEqual(len(pending), 1)
        saved = json.loads(pending[0].read_text())
        self.assertEqual(saved["payload"]["recommended_route"], "waiting-for-user")

        with patch.object(result_contract, "normalize", side_effect=AssertionError("must reuse saved normalization")):
            self.assertEqual(result.replay_pending_results(db_path)["recorded"], 1)
            self.assertTrue(result.record_result(db_path, payload)["deduplicated"])
        with closing(sqlite3.connect(db_path)) as conn:
            rows = conn.execute("SELECT metadata_json FROM worker_results").fetchall()
            self.assertEqual(len(rows), 1)
            metadata = json.loads(rows[0][0])
            self.assertEqual(metadata["recommended_route"], "waiting-for-user")
            self.assertEqual(
                metadata["normalized_payload"],
                {key: value for key, value in saved["payload"].items() if not key.startswith("_")},
            )

    def test_result_recording_allows_no_external_skills(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db(output_type="local_result", route_id="local-result")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="analysis complete",
            used_skills=[],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)

    def test_classification_result_records_recommended_route(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db(output_type="classification_result", route_id="classification-result")
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="classification_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="Recommend route new-pr",
            used_skills=[],
            recommended_route="new-pr",
            branch_slug="model-service-connectivity-test",
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id = ?",
                    (record["result_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(payload["submission"]["recommended_route"], "new-pr")
        self.assertEqual(metadata["recommended_route"], "new-pr")
        self.assertEqual(payload["submission"]["branch_slug"], "model-service-connectivity-test")
        self.assertEqual(metadata["branch_slug"], "model-service-connectivity-test")

    def test_classification_result_rejects_invalid_branch_slug(self):
        from robert_agent.delivery.application import audit as audit_result

        payload = internal_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="classification_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="Recommend route new-pr",
            used_skills=[],
            recommended_route="new-pr",
            branch_slug="Model Service Connectivity Test",
        )

        audit = audit_result.audit_result(
            payload,
            allowed_github_actions=[],
            expected_output="classification_result",
        )

        self.assertEqual(audit["status"], "failed")
        self.assertIn("branch_slug", audit["safe_error"])

    def test_result_records_used_skills_metadata(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[
                {
                    "command": "python -m unittest",
                    "status": "passed",
                    "purpose": "regression",
                    "required": False,
                    "exit_code": 0,
                    "source": "worker_reported",
                }
            ],
            handoff="done",
            used_skills=["fast-code-path"],
            review_point_evaluation=[self._review_point("correct", "comment")],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
        self.assertEqual(
            json.loads(metadata)["used_skills"],
            ["fast-code-path"],
        )
        self.assertEqual(
            json.loads(metadata)["review_point_evaluation"],
            [self._review_point("correct", "comment")],
        )

    def test_result_records_remediation_evidence_metadata(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db(output_type="update_existing_pr", route_id="update-existing-pr")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET task_kind='ci_remediation', metadata_json=json_object('remediation_episode_id','episode-ci')"
            )
            conn.execute("UPDATE github_sources SET source_type='pull_request',source_key='x/y!1'")
            conn.execute("""INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,
                observed_head_sha,observed_base_sha,status,failure_signature,first_seen_at,updated_at)
                VALUES('episode-ci','ws-1','ci','ci-1','head-1','base-1','remediating','failure-1','now','now')""")
        evidence = {
            "kind": "ci",
            "episode_id": "episode-ci",
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "failure_signature": "failure-1",
            "resolution_summary": "Adjusted the assertion and added coverage.",
        }
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[{"type": "comment", "body": "CI correction completed."}],
            consumed_event_fingerprints=["github-system:workflow_run:101:1"],
            verification=[],
            handoff="fixed CI",
            used_skills=["fast-test-fix", "fast-preflight"],
            remediation_evidence={"resolution_summary": evidence["resolution_summary"]},
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])
        self.assertEqual(metadata["remediation_evidence"], evidence)

    def test_result_records_memory_metadata(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        memory_delta = {
            "status": "has_memory",
            "entries": [
                {
                    "operation": "upsert",
                    "kind": "decision",
                    "title": "Keep PR workstreams separate from issue workstreams",
                    "short_summary": "DD PR follow-up comments update the PR workstream.",
                    "long_summary": "The PR workstream remains linked to the origin issue, but it has its own active task mutex.",
                    "paths": ["src/robert_agent/runtime/cycle.py"],
                    "symbols": ["_create_task_attempt_and_prompt"],
                    "keywords": ["workstream", "dd-pr-followup"],
                    "confidence": "medium",
                }
            ],
        }
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[
                {
                    "command": "python -m unittest",
                    "status": "passed",
                    "purpose": "regression",
                    "required": False,
                    "exit_code": 0,
                    "source": "worker_reported",
                }
            ],
            handoff="done",
            used_skills=["fast-code-path"],
            memory_delta=memory_delta,
            used_memory_ids=["pmem-existing"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])
        self.assertEqual(metadata["memory_delta"], memory_delta)
        self.assertEqual(metadata["used_memory_ids"], ["pmem-existing"])

    def test_result_records_default_memory_metadata(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])
        self.assertEqual(
            metadata["memory_delta"],
            {
                "status": "none",
                "reason": "worker did not provide reusable project memory",
            },
        )
        self.assertEqual(metadata["used_memory_ids"], [])

    def test_result_records_planned_actions_as_unpublished_intents(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[
                {
                    "type": "comment",
                    "target_url": "https://github.com/x/y/issues/1",
                    "body": "Looks good",
                }
            ],
            consumed_event_fingerprints=["comment:1"],
            verification=[
                {
                    "command": "python -m unittest",
                    "status": "passed",
                    "purpose": "regression",
                    "required": False,
                    "exit_code": 0,
                    "source": "worker_reported",
                }
            ],
            handoff="ready to comment",
            used_skills=["fast-code-path"],
        )

        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT action_type, target_url, external_id, audit_status, publish_status, metadata_json
                FROM github_actions
                """
            ).fetchone()
        self.assertEqual(action[:5], ("comment", "https://github.com/x/y/issues/1", None, "pending", "not_published"))
        self.assertTrue(json.loads(action[5])["body"].startswith("Looks good\n\n<!-- robert-comment"))

    def test_result_rejects_missing_used_skills(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "comment_analysis",
            "planned_github_actions": [{"type": "comment", "body": "Completed analysis."}],
            "consumed_event_fingerprints": ["comment:1"],
            "verification": [],
            "handoff": "missing skill evidence",
        }

        payload = {
            "task_id": payload.pop("task_id"),
            "attempt_id": payload.pop("attempt_id"),
            "output_type": payload.pop("output_type"),
            "submission": payload,
        }
        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertIn("used_skills", record["safe_error"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_result_rejects_legacy_actual_actions_key(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "comment_analysis",
            "actual_github_actions": [{"type": "comment", "body": "Completed analysis."}],
            "consumed_event_fingerprints": ["comment:1"],
            "used_skills": ["fast-code-path"],
            "verification": [],
            "handoff": "legacy action payload",
        }

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertEqual(record["reason_code"], "invalid_envelope")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_stale_attempt_result_is_recorded_for_audit(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status = 'stale' WHERE attempt_id = 'attempt-1'")

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="recovered and completed",
            used_skills=["fast-code-path"],
        )
        record = result.record_result(db_path, payload)

        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute("SELECT status FROM attempts WHERE attempt_id = 'attempt-1'").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(attempt_status, "completed")
        self.assertEqual(result_count, 1)
        self.assertEqual(action_count, 1)

    def test_result_rejects_action_without_type(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="update_existing_pr",
            planned_github_actions=[{"action": "push_existing_pr", "url": "https://github.com/x/y/pull/1"}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="updated existing pr",
        )

        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertEqual(record["field_path"], "planned_github_actions[0].type")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_result_rejects_supervisor_failed_attempt_without_pending_actions(self):
        from robert_agent.delivery.application import submission as result

        db_path = self._init_attempt_db()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE attempt_id = 'attempt-1'",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.execute("UPDATE tasks SET lifecycle = 'failed' WHERE task_id = 'task-1'")
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'failed', active_task_id = NULL WHERE workstream_id = 'ws-1'"
            )

        payload = result.build_result(
            task_id="task-1",
            attempt_id="attempt-1",
            output_type="comment_analysis",
            planned_github_actions=[{"type": "comment", "body": "Completed analysis."}],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="late result",
            used_skills=["fast-code-path"],
        )
        record = result.record_result(db_path, payload)

        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "rejected_by_supervisor")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute("SELECT status FROM attempts WHERE attempt_id = 'attempt-1'").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0]
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(result_count, 0)
        self.assertEqual(action_count, 0)

    def test_result_rejects_unknown_task_or_attempt(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "dd.sqlite3"
        storage.init_database(db_path)

        payload = result.build_result(
            task_id="missing-task",
            attempt_id="missing-attempt",
            output_type="local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="done",
            used_skills=["fast-code-path"],
        )

        self.assertEqual(result.record_result(db_path, payload)["status"], "rejected_by_supervisor")

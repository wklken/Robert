from contextlib import closing
import json
import sqlite3
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _dd_comment_body,
    _review_point_evaluation,
    _verification_evidence,
)


class CycleResultAuditTests(RepositoryCycleFixture):
    def test_run_once_rejects_publishable_result_without_required_verification(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [],
                    "handoff": "opened PR without verification",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertEqual(record["reason_code"], "invalid_verification")
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "prepared")

    def test_audit_rejects_comment_target_outside_task_repository(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis-scope",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "submission": {
                    "planned_github_actions": [
                        {
                            "type": "comment",
                            "target_url": "https://github.com/other/repo/issues/999",
                            "body": _dd_comment_body(
                                "Analysis is ready",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                fingerprints="comment:comment-analysis-scope",
                            ),
                        }
                    ],
                    "consumed_event_fingerprints": ["comment:comment-analysis-scope"],
                    "verification": [],
                    "handoff": "ready",
                    "used_skills": ["fast-code-path"],
                },
            },
        )
        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertEqual(record["reason_code"], "action_scope")
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "prepared")

    def test_worker_result_missing_recommended_skill_is_accepted(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "opened PR",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone()
            action_status = conn.execute("SELECT audit_status, publish_status FROM github_actions").fetchone()
            audit_metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["audit"]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(action_status, ("accepted", "not_published"))
        self.assertEqual(audit_metadata["status"], "accepted")
        self.assertIsNone(notification)

    def test_update_existing_pr_audit_uses_required_skill_and_review_evaluation(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        pr_fixture = self.root / "pr-followup.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-followup",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this review point",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/dd-123-task",
                            "existing_pr_head_sha": "worker-base-sha",
                            "pr_author_login": "robert-bot",
                            "url": "https://github.com/example/backend/pull/456#discussion_r1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
            repo_id, source_id = conn.execute("SELECT repo_id, primary_source_id FROM workstreams").fetchone()
            conn.execute(
                """
                INSERT INTO github_events(
                  event_id, repo_id, source_id, event_fingerprint, event_type,
                  actor_login, authorization_status, event_at, payload_json
                ) VALUES (?, ?, ?, ?, 'comment', 'wklken',
                          'authorized_trigger', ?, ?)
                """,
                (
                    "event-human-update",
                    repo_id,
                    source_id,
                    "comment:human-update",
                    "2099-01-01T00:00:00Z",
                    json.dumps({"existing_pr_head_sha": "human-new-sha"}),
                ),
            )
            required_skills, recommended_skills = conn.execute(
                "SELECT required_skills_json, recommended_skills_json FROM route_decisions"
            ).fetchone()
        self.assertEqual(json.loads(required_skills), [])
        self.assertIn("fast-verify-review-point", json.loads(recommended_skills))

        self._materialize_worktree(self.worktree_root / "codex__dd-123-task", "codex/dd-123-task")
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "update_existing_pr",
                "submission": {
                    "planned_github_actions": [
                        {"type": "push_existing_pr"},
                        {
                            "type": "comment",
                            "target_url": "https://github.com/example/backend/pull/456",
                            "body": _dd_comment_body("Implemented the valid review point and pushed the branch."),
                        },
                    ],
                    "consumed_event_fingerprints": ["comment:pr-followup"],
                    "verification": [_verification_evidence()],
                    "handoff": "updated existing PR",
                    "used_skills": ["fast-verify-review-point"],
                    "review_input_kind": "review_feedback",
                    "review_point_evaluation": _review_point_evaluation(),
                },
            },
        )
        self.assertTrue(record["ok"], record)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action_statuses = conn.execute(
                "SELECT action_type, audit_status, publish_status FROM github_actions ORDER BY action_type"
            ).fetchall()
            audit_metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["audit"]
        self.assertEqual(
            action_statuses,
            [
                ("comment", "accepted", "not_published"),
                ("push_existing_pr", "accepted", "not_published"),
            ],
        )
        self.assertEqual(audit_metadata["status"], "accepted")
        self.assertEqual(
            audit_metadata["action_scope"]["existing_pr_head_sha"],
            "worker-base-sha",
        )

    def test_worker_result_output_type_must_match_route_expected_output(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "submission": {
                    "planned_github_actions": [
                        {
                            "type": "comment",
                            "target_url": "https://github.com/example/backend/issues/123",
                            "body": _dd_comment_body(
                                "Analysis only",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                fingerprints="comment:comment-1",
                            ),
                        }
                    ],
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [],
                    "handoff": "analysis only",
                    "used_skills": [],
                },
            },
        )
        self.assertFalse(record["ok"], record)
        self.assertEqual(record["status"], "failed_validation")
        self.assertEqual(record["reason_code"], "unexpected_output_type")
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "prepared")

    def test_worker_result_is_not_reaudited_after_audit_metadata_recorded(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "opened PR",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE route_decisions
                SET allowed_github_actions_json = '[]'
                WHERE task_id = ?
                """,
                (task_id,),
            )

        reaudit = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(reaudit["ok"], reaudit)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            action_status = conn.execute("SELECT audit_status, publish_status FROM github_actions").fetchone()
            audit_metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["audit"]
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(action_status, ("accepted", "not_published"))
        self.assertEqual(audit_metadata["status"], "accepted")

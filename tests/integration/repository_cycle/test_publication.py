from robert_agent.delivery.application import publish
from contextlib import closing
import json
import sqlite3
from tests.support import PACKAGE_ROOT
from robert_agent.work.application import completion
from robert_agent.application import repository_cycle
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _dd_comment_body,
    _verification_evidence,
)


class CyclePublicationTests(RepositoryCycleFixture):
    def test_audited_worker_result_with_unpublished_action_keeps_task_active(self):
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
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
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
            wakeup_status = conn.execute(
                "SELECT status FROM wakeups WHERE result_id = ?",
                (record["result_id"],),
            ).fetchone()[0]
            relationship = conn.execute(
                "SELECT relationship FROM task_events"
            ).fetchone()[0]
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(action_status, ("accepted", "not_published"))
        self.assertEqual(wakeup_status, "consumed")
        self.assertEqual(relationship, "trigger")

    def test_run_once_records_publish_actions_step_after_audit(self):
        from robert_agent.runtime import cycle as run_once

        calls = []
        original_publish = publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            calls.append((str(db_path), dry_run))
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            publish.publish_ready_actions = fake_publish_ready_actions
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            publish.publish_ready_actions = original_publish

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, [(str(self.data_dir / "dd.sqlite3"), False)])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            step = conn.execute(
                """
                SELECT status, output_json
                FROM run_steps
                WHERE step_key = 'publish_actions'
                """
            ).fetchone()
        self.assertEqual(step[0], "succeeded")
        self.assertEqual(json.loads(step[1])["published_count"], 1)

    def test_skip_publish_skips_publish_step_and_does_not_invoke_publish(self):
        from robert_agent.runtime import cycle as run_once

        original_publish = publish.publish_ready_actions
        original_finalize = completion.finalize_published_results

        def unexpected_publish(*_args, **_kwargs):
            raise AssertionError("publish should not run when skip_publish=True")

        def unexpected_finalize(*_args, **_kwargs):
            raise AssertionError("finalize should not run when skip_publish=True")

        try:
            publish.publish_ready_actions = unexpected_publish
            completion.finalize_published_results = unexpected_finalize
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
                skip_publish=True,
            )
        finally:
            publish.publish_ready_actions = original_publish
            completion.finalize_published_results = original_finalize

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            step = conn.execute(
                """
                SELECT status, output_json
                FROM run_steps
                WHERE step_key = 'publish_actions'
                """
            ).fetchone()
        self.assertEqual(step[0], "skipped")
        self.assertEqual(json.loads(step[1])["reason"], "skip_publish")

    def test_publish_step_finalizes_task_after_action_is_published(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
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
                                "Analysis is ready",
                                task_id=task_id,
                                attempt_id=attempt_id,
                                fingerprints="comment:comment-analysis",
                            ),
                        }
                    ],
                    "consumed_event_fingerprints": ["comment:comment-analysis"],
                    "verification": [_verification_evidence()],
                    "handoff": "ready",
                    "used_skills": ["fast-code-path"],
                },
            },
        )
        self.assertTrue(record["ok"], record)

        original_publish = publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE github_actions
                    SET publish_status = 'published',
                        external_id = '987',
                        target_url = 'https://github.com/example/backend/issues/123#issuecomment-987'
                    WHERE result_id = ?
                    """,
                    (record["result_id"],),
                )
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            publish.publish_ready_actions = fake_publish_ready_actions
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            publish.publish_ready_actions = original_publish

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            relationship = conn.execute(
                "SELECT relationship FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            publish_step = json.loads(
                conn.execute(
                    "SELECT output_json FROM run_steps WHERE step_key = 'publish_actions' ORDER BY started_at DESC LIMIT 1"
                ).fetchone()[0]
            )
        self.assertEqual(task_lifecycle, "completed")
        self.assertEqual(workstream, ("completed", None))
        self.assertEqual(relationship, "consumed")
        self.assertEqual(publish_step["finalized_task_count"], 1)

        from robert_agent.readmodels import board

        with closing(sqlite3.connect(db_path)) as conn:
            item_id, completed_at = conn.execute(
                "SELECT work_item_id, completed_at FROM work_items"
            ).fetchone()
        self.assertIsNotNone(completed_at)
        self.assertEqual(board.get_work_item_detail(db_path, item_id)["column"], "done")

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "analysis-followup",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "assignment_actor_login": "wklken",
                            "body": "Please analyze the token flow too",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        followup = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(followup["ok"], followup)
        detail = board.get_work_item_detail(db_path, item_id)
        self.assertEqual(detail["column"], "todo")
        self.assertIsNone(detail["completed_at"])
        self.assertNotEqual(detail["task_id"], task_id)

    def test_publish_failure_does_not_skip_unrelated_routing(self):
        from robert_agent.runtime import cycle as run_once

        original_publish = publish.publish_ready_actions

        def fake_publish_ready_actions(_db_path, dry_run=False, repo_id=None):
            return {
                "ok": False,
                "status": "publish_failed",
                "pending_count": 1,
                "published_count": 0,
                "deduplicated_count": 0,
                "skipped_count": 0,
                "failed_count": 1,
                "failures": [
                    {"action_id": "action-old", "safe_error": "old PR already exists"}
                ],
            }

        try:
            publish.publish_ready_actions = fake_publish_ready_actions
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        finally:
            publish.publish_ready_actions = original_publish

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "publish_failed")
        self.assertEqual(len(result["prompt_paths"]), 1)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            step_statuses = dict(conn.execute("SELECT step_key, status FROM run_steps"))
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(step_statuses["publish_actions"], "failed")
        self.assertEqual(step_statuses["discover"], "succeeded")
        self.assertEqual(step_statuses["authorize"], "succeeded")
        self.assertEqual(step_statuses["route"], "succeeded")
        self.assertEqual(step_statuses["dispatch"], "succeeded")
        self.assertEqual(step_statuses["summarize"], "succeeded")
        self.assertEqual(task_count, 1)

    def test_open_pr_result_materializes_published_pr_target_linked_to_origin_issue(
        self,
    ):
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

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
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET publish_status = 'published',
                    target_url = CASE
                      WHEN action_type = 'open_pr'
                      THEN 'https://github.com/example/backend/pull/10'
                      ELSE target_url
                    END
                WHERE result_id = ?
                """,
                (record["result_id"],),
            )

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            pr_workstream = conn.execute(
                """
                SELECT workstream_id, origin_workstream_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!10'
                """
            ).fetchone()
        self.assertEqual(
            pr_workstream,
            (
                "github:example/backend!10",
                "github:example/backend#123",
            ),
        )

    def test_run_once_publish_filters_single_config_repo_when_db_has_other_repo_actions(
        self,
    ):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for repo_id, full_name in [
                ("repo:example/backend", "example/backend"),
                ("repo:other", "Org/other"),
            ]:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, 'robot', 'main', ?, ?)",
                    (repo_id, full_name, str(self.repo_root), str(self.worktree_root)),
                )
                source_id = f"source:{repo_id}"
                workstream_id = f"github:{full_name}#1"
                task_id = f"task:{repo_id}"
                result_id = f"result:{repo_id}"
                conn.execute(
                    "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) VALUES (?, ?, ?, 'issue', 1)",
                    (source_id, repo_id, workstream_id),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                    (workstream_id, repo_id, source_id, now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'completed', ?, ?)",
                    (task_id, workstream_id, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, finished_at, metadata_json) VALUES (?, ?, 1, 'completed', ?, ?, '{\"result_contract\":\"compact-v1\"}')",
                    (f"attempt:{repo_id}", task_id, now, now),
                )
                conn.execute(
                    "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at, metadata_json) VALUES (?, ?, ?, 'comment_analysis', ?, json_object('result_contract','compact-v1'))",
                    (result_id, task_id, f"attempt:{repo_id}", now),
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, publish_status, created_at, metadata_json) VALUES (?, ?, ?, 'comment', ?, 'accepted', 'not_published', ?, ?)",
                    (
                        f"action:{repo_id}",
                        result_id,
                        task_id,
                        f"https://github.com/{full_name}/issues/1",
                        now,
                        json.dumps(
                            {"body": "[from-codex local repo] hi"}, sort_keys=True
                        ),
                    ),
                )

        result = repository_cycle._publish_ready_actions_for_repo(
            db_path,
            dry_run=True,
            repo_id="repo:example/backend",
            repo_count=1,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pending_count"], 1)

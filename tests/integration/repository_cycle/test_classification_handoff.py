from contextlib import closing
import json
import sqlite3
import yaml
from pathlib import Path
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class ClassificationHandoffTests(RepositoryCycleFixture):
    def test_default_waiting_route_preserves_new_trusted_request(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle

        assignment = {
            "id": "assign-pending",
            "number": 2904,
            "source_type": "issue",
            "event_type": "assigned",
            "actor_login": "wklken",
            "body": "@robert-bot please take a look",
            "intent": "unclear",
            "url": "https://github.com/example/backend/issues/2904",
            "state": "open",
        }
        self.fixture_path.write_text(json.dumps({"events": [assignment]}), encoding="utf-8")
        first = cycle.run_once(self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        reply = {
            **assignment,
            "id": "clarify-pending",
            "event_type": "comment",
            "body": "@robert-bot please analyze the current behavior",
            "intent": "analysis",
        }
        self.fixture_path.write_text(json.dumps({"events": [reply]}), encoding="utf-8")
        appended = cycle.run_once(self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(appended["ok"], appended)
        receipt = result.record_result(
            db_path,
            result.build_result(
                task_id=task_id,
                attempt_id=attempt_id,
                output_type="classification_result",
                consumed_event_fingerprints=["assigned:assign-pending"],
                verification=[],
                handoff="The requested outcome is unclear.",
            ),
        )
        self.assertTrue(receipt["ok"], receipt)
        finalized = cycle.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn:
            child = conn.execute("SELECT task_id, route_id FROM tasks WHERE parent_task_id = ?", (task_id,)).fetchone()
            self.assertEqual(child[1], "comment-analysis")
            self.assertEqual(
                conn.execute(
                    "SELECT event_id FROM task_events WHERE task_id = ? AND relationship = 'trigger'", (child[0],)
                ).fetchone()[0],
                "event:comment:clarify-pending",
            )

    def test_classification_without_route_creates_one_question_task(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "body": "@robert-bot please classify this",
                            "intent": "unclear",
                            "title": "Bug needs implementation",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
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
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["assigned:assign-2904"],
                    "verification": [],
                    "handoff": "Recommend route new-pr",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn:
            child = conn.execute(
                "SELECT task_id, route_id, expected_output FROM tasks WHERE parent_task_id = ?", (task_id,)
            ).fetchone()
            self.assertIsNotNone(child)
            self.assertEqual(child[1:], ("waiting-for-user", "waiting_for_user"))
            self.assertEqual(
                json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["recommended_route"],
                "waiting-for-user",
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertIsNone(
                conn.execute("SELECT worktree_path FROM attempts WHERE task_id = ?", (child[0],)).fetchone()[0]
            )

        repeated = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(repeated["ok"], repeated)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            child_attempt = conn.execute("SELECT attempt_id FROM attempts WHERE task_id = ?", (child[0],)).fetchone()[0]

        question = result.record_result(
            db_path,
            result.build_result(
                task_id=child[0],
                attempt_id=child_attempt,
                output_type="waiting_for_user",
                consumed_event_fingerprints=["assigned:assign-2904"],
                verification=[],
                handoff="Waiting for the requested outcome.",
                planned_github_actions=[{"type": "comment", "body": "Do you want analysis or implementation?"}],
                operator_question={"kind": "clarification", "summary": "Do you want analysis or implementation?"},
            ),
        )
        self.assertTrue(question["ok"], question)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            # Supply the remote publication observation without posting a real comment.
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?", (question["result_id"],)
            )
        waiting = run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        self.assertTrue(waiting["ok"], waiting)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone(),
                ("waiting_for_user", child[0]),
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM work_item_events WHERE event_type = 'operator_question'").fetchone()[
                    0
                ],
                1,
            )

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "clarify-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze the current behavior",
                            "intent": "analysis",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        resumed = run_once.run_once(self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(resumed["ok"], resumed)
        with closing(sqlite3.connect(db_path)) as conn:
            successor = conn.execute("SELECT route_id FROM tasks WHERE parent_task_id = ?", (child[0],)).fetchone()
            self.assertEqual(successor, ("comment-analysis",))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    def test_classification_result_child_keeps_canonical_trigger_payload(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "assignment_actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-2904",
                            "body": "背景：dashboard admin missing fields\n需求：add readonly fields",
                            "intent": "unclear",
                            "title": "Bug needs implementation",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
                        },
                        {
                            "id": "notification-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "notification",
                            "actor_login": "",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-2904",
                            "body": "",
                            "intent": "unclear",
                            "title": "Bug needs implementation",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
                        },
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
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["assigned:assign-2904"],
                    "verification": [],
                    "handoff": "Recommend route new-pr",
                    "recommended_route": "new-pr",
                    "branch_slug": "canonical-trigger",
                    "used_skills": [],
                },
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            child_task_id = conn.execute(
                """
                SELECT task_id
                FROM tasks
                WHERE parent_task_id = ?
                """,
                (task_id,),
            ).fetchone()[0]
            context_path = conn.execute(
                """
                SELECT path
                FROM artifacts
                WHERE task_id = ?
                  AND artifact_type = 'github_context_json'
                """,
                (child_task_id,),
            ).fetchone()[0]
        child_context = json.loads(Path(context_path).read_text(encoding="utf-8"))
        child_event = child_context["events"][0]
        self.assertEqual(child_event["event_type"], "assigned")
        self.assertEqual(child_event["authorization_status"], "authorized_trigger")
        self.assertEqual(
            child_event["body"],
            "背景：dashboard admin missing fields\n需求：add readonly fields",
        )

    def test_classification_recommended_update_existing_pr_reuses_prior_pr_branch(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-conflict-followup",
                            "number": 2886,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "comment:pr-conflict-followup",
                            "body": "@robert-bot 有冲突",
                            "intent": "unclear",
                            "title": "fix(operator): improve release log context",
                            "url": "https://github.com/example/backend/pull/2886",
                            "state": "open",
                            "has_open_dd_pr": False,
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
            task_id, attempt_id, workstream_id = conn.execute(
                """
                SELECT a.task_id, a.attempt_id, t.workstream_id
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                """
            ).fetchone()
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES ('task-pr-existing', ?, 'completed', 'P1', 'update-existing-pr', 'update_existing_pr', ?, ?)
                """,
                (
                    workstream_id,
                    "2026-06-17T08:00:00+00:00",
                    "2026-06-17T08:30:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at, finished_at, metadata_json)
                VALUES ('attempt-pr-existing', 'task-pr-existing', 1, 'completed', ?, 'codex/issue-2884-fix-operator', ?, ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (
                    str(self.worktree_root / "codex__issue-2884-fix-operator"),
                    "2026-06-17T08:00:00+00:00",
                    "2026-06-17T08:30:00+00:00",
                    "2026-06-17T08:30:00+00:00",
                ),
            )
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["comment:pr-conflict-followup"],
                    "verification": [],
                    "handoff": "PR #2886 has conflicts; route to update-existing-pr and reuse the current PR branch.",
                    "used_skills": [],
                    "recommended_route": "update-existing-pr",
                },
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT child.route_id, child.expected_output, a.branch_name, a.worktree_path
                FROM tasks child
                JOIN attempts a ON a.task_id = child.task_id
                WHERE child.parent_task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(row[0], "update-existing-pr")
        self.assertEqual(row[1], "update_existing_pr")
        self.assertEqual(row[2], "codex/issue-2884-fix-operator")
        self.assertIn("codex__issue-2884-fix-operator", row[3])

    def test_completed_classification_result_with_consumed_trigger_recovers_child_task(
        self,
    ):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.delivery.application import submission as result

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-16",
                            "number": 16,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "robert-bot",
                            "assignment_actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-16",
                            "body": "remove the DCO check from GitHub Actions",
                            "intent": "unclear",
                            "title": "remove the dco in ci",
                            "url": "https://github.com/example/backend/issues/16",
                            "state": "open",
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
        with closing(sqlite3.connect(db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["assigned:assign-16"],
                    "verification": [],
                    "handoff": "Route to an implementation PR",
                    "used_skills": [],
                    "recommended_route": "new-pr",
                    "branch_slug": "remove-dco-from-ci",
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = json_set(metadata_json, '$.audit.status', 'accepted', '$.audit.ok', json('true'))"
            )
            conn.execute(
                "UPDATE task_events SET relationship = 'consumed' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'active', active_task_id = ?",
                (task_id,),
            )

        recovered = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["dispatch_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn:
            tasks = conn.execute(
                """
                SELECT task_id, parent_task_id, lifecycle, route_id, expected_output
                FROM tasks
                ORDER BY created_at, task_id
                """
            ).fetchall()
            branch_name = conn.execute(
                """
                SELECT a.branch_name
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE t.parent_task_id = ?
                """,
                (task_id,),
            ).fetchone()[0]
            child_relationship = conn.execute(
                """
                SELECT te.relationship
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                  AND ge.event_fingerprint = 'assigned:assign-16'
                """,
                (tasks[1][0],),
            ).fetchone()[0]
            workstream = conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0][2], "completed")
        self.assertEqual(tasks[1][1], tasks[0][0])
        self.assertEqual(tasks[1][2:], ("queued", "new-pr", "new_pr"))
        self.assertEqual(branch_name, "codex/dd-16-remove-dco-from-ci")
        self.assertEqual(child_relationship, "trigger")
        self.assertEqual(workstream, ("active", tasks[1][0]))

    def test_recovered_classification_result_uses_consumed_trigger_not_older_context(
        self,
    ):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.delivery.application import submission as result

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "context-old",
                            "number": 15,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "comment:context-old",
                            "body": "older context that should not drive the implementation task",
                            "intent": "unclear",
                            "title": "older issue context",
                            "url": "https://github.com/example/backend/issues/15#issuecomment-1",
                            "state": "open",
                            "event_at": "2026-07-30T08:00:00Z",
                            "drives_execution": False,
                            "creates_task": False,
                        },
                        {
                            "id": "assign-16",
                            "number": 16,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "robert-bot",
                            "assignment_actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-16",
                            "body": "remove the DCO check from GitHub Actions",
                            "intent": "unclear",
                            "title": "remove the dco in ci",
                            "url": "https://github.com/example/backend/issues/16",
                            "state": "open",
                            "event_at": "2026-07-30T08:32:30Z",
                        },
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
            context_event_id = conn.execute(
                "SELECT event_id FROM github_events WHERE event_fingerprint = 'comment:context-old'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO task_events (task_id, event_id, relationship, created_at) VALUES (?, ?, 'context', datetime('now'))",
                (task_id, context_event_id),
            )
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["assigned:assign-16"],
                    "verification": [],
                    "handoff": "Route to an implementation PR",
                    "used_skills": [],
                    "recommended_route": "new-pr",
                    "branch_slug": "remove-dco-from-ci",
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json = json_set(metadata_json, '$.audit.status', 'accepted', '$.audit.ok', json('true'))"
            )
            conn.execute(
                "UPDATE task_events SET relationship = 'consumed' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'active', active_task_id = ?",
                (task_id,),
            )

        recovered = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(recovered["ok"], recovered)
        with closing(sqlite3.connect(db_path)) as conn:
            child_task_id = conn.execute(
                "SELECT task_id FROM tasks WHERE parent_task_id = ?",
                (task_id,),
            ).fetchone()[0]
            child_context_path = conn.execute(
                """
                SELECT path
                FROM artifacts
                WHERE task_id = ?
                  AND artifact_type = 'github_context_json'
                """,
                (child_task_id,),
            ).fetchone()[0]
            branch_name = conn.execute(
                """
                SELECT a.branch_name
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE t.task_id = ?
                """,
                (child_task_id,),
            ).fetchone()[0]
        child_context = json.loads(Path(child_context_path).read_text(encoding="utf-8"))
        child_event = child_context["events"][0]
        self.assertEqual(child_event["event_fingerprint"], "assigned:assign-16")
        self.assertEqual(child_event["body"], "remove the DCO check from GitHub Actions")
        self.assertEqual(branch_name, "codex/dd-16-remove-dco-from-ci")

    def test_classification_result_branch_slug_names_child_worktree(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.delivery.application import submission as result

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-3022",
                            "number": 3022,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "assignment_actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-3022",
                            "body": "实现模型服务连通性测试并拉取支持的模型列表",
                            "intent": "unclear",
                            "title": "模型服务连通性测试",
                            "url": "https://github.com/example/backend/issues/3022",
                            "state": "open",
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
        with closing(sqlite3.connect(db_path)) as conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": ["assigned:assign-3022"],
                    "verification": [],
                    "handoff": "Implement the requested connectivity test",
                    "used_skills": [],
                    "recommended_route": "new-pr",
                    "branch_slug": "model-service-connectivity-test",
                },
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn:
            branch_name = conn.execute(
                """
                SELECT a.branch_name
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE t.parent_task_id = ?
                """,
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(branch_name, "codex/dd-3022-model-service-connectivity-test")

    def test_classification_result_uses_selected_worker_defaults_and_route_override(
        self,
    ):
        from robert_agent.runtime import cycle as run_once

        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["classifier"] = {
            "adapter": "codex",
            "command": "/opt/homebrew/bin/codex",
            "model": "gpt-5.6-sol",
            "effort": "medium",
        }
        config["routes"] = {"classification-result": {"worker": "classifier", "effort": "xhigh"}}
        self.config_path.write_text(yaml.safe_dump(config))
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "unclear-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please classify this",
                            "intent": "unclear",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            route_id, metadata_json = conn.execute(
                """
                SELECT t.route_id, a.metadata_json
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                """
            ).fetchone()
        command = json.loads(metadata_json)["dispatch"]["command"]
        self.assertEqual(route_id, "classification-result")
        self.assertEqual(command[0], "/opt/homebrew/bin/codex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="xhigh"', command)

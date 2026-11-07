from contextlib import closing
import json
import sqlite3
from pathlib import Path
from tests.support import PACKAGE_ROOT
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution import workspace as assigned_workspace
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class PreparedPRFollowupTests(RepositoryCycleFixture):
    def test_robot_authored_pr_followup_without_hidden_metadata_reuses_existing_branch(self):
        from robert_agent.runtime import cycle as run_once

        fixture_path = self.root / "pr-followup.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-followup-1",
                            "number": 2883,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot 处理下\n\nRefs #2878",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2878-mcp-proxy",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#2878",
                                    "source_issue": "2878",
                                    "inferred_from": "pr_body_issue_reference",
                                }
                            },
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT t.workstream_id, t.route_id, t.expected_output, a.branch_name, a.worktree_path
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                """
            ).fetchone()
        self.assertEqual(row[0], "github:example/backend!2883")
        self.assertEqual(row[1], "update-existing-pr")
        self.assertEqual(row[2], "update_existing_pr")
        self.assertEqual(row[3], "codex/issue-2878-mcp-proxy")
        self.assertIn("codex__issue-2878-mcp-proxy", row[4])

    def test_dd_pr_followup_trigger_is_prioritized_over_older_issue_workstream(self):
        from robert_agent.runtime import cycle as run_once

        fixture_path = self.root / "priority-followup.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "older-issue-task",
                            "number": 2878,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                            "event_at": "2026-06-17T02:30:00Z",
                        },
                        {
                            "id": "pr-followup-task",
                            "number": 2883,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot 处理下",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2878-mcp-proxy",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#2878",
                                    "source_issue": "2878",
                                    "inferred_from": "pr_body_issue_reference",
                                }
                            },
                            "event_at": "2026-06-17T02:38:44Z",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT t.workstream_id, t.route_id, t.expected_output, a.branch_name
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                ORDER BY CASE WHEN t.workstream_id LIKE 'github:example/backend!%' THEN 0 ELSE 1 END,
                         t.created_at,
                         t.task_id
                """
            ).fetchall()
            pending = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                ORDER BY te.relationship, ge.event_fingerprint
                """
            ).fetchall()
        self.assertEqual(
            row,
            [
                (
                    "github:example/backend!2883",
                    "update-existing-pr",
                    "update_existing_pr",
                    "codex/issue-2878-mcp-proxy",
                ),
                (
                    "github:example/backend#2878",
                    "comment-analysis",
                    "comment_analysis",
                    None,
                ),
            ],
        )
        self.assertEqual(
            pending,
            [("trigger", "comment:older-issue-task"), ("trigger", "comment:pr-followup-task")],
        )

    def test_prepared_pr_reroute_preserves_required_skill_gate(self):
        with self.config_path.open("a") as config:
            config.write("\nroutes:\n  update-existing-pr:\n    required_skills: [missing-reroute-skill]\n")
        self.test_active_prepared_pr_task_is_rerouted_when_pr_followup_context_arrives(blocked=True)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            row = conn.execute(
                "SELECT t.lifecycle, a.status, rd.required_skills_json "
                "FROM tasks t JOIN attempts a ON a.task_id=t.task_id "
                "JOIN route_decisions rd ON rd.task_id=t.task_id "
                "WHERE t.task_id='task-pr-2886'"
            ).fetchone()
            self.assertEqual(row[:2], ("failed", "failed"))
            self.assertIn("missing-reroute-skill", json.loads(row[2]))
            from robert_agent.execution.application.dispatch import current_dispatch_target

            self.assertIsNone(current_dispatch_target(conn, "attempt-pr-2886"))

    def test_active_prepared_pr_task_is_rerouted_when_pr_followup_context_arrives(self, blocked=False):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        task_id = "task-pr-2886"
        attempt_id = "attempt-pr-2886"
        prompt_path = self.data_dir / "artifacts" / task_id / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("stale prompt", encoding="utf-8")
        now = "2026-06-17T09:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "repo:example/backend",
                    "example/backend",
                    "robert-bot",
                    "master",
                    str(self.repo_root),
                    str(self.worktree_root),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend!2886', 'repo:example/backend', 'github:example/backend!2886', 'pull_request', 2886, 'fix(operator): improve release log context', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend!2886', 'repo:example/backend', 'source:github:example/backend!2886', 'github:example/backend#2884', 'active', ?, ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES (?, 'github:example/backend!2886', 'queued', 'P1', 'new-pr', 'new_pr', ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO route_decisions(route_decision_id, task_id, route_id, expected_output, allowed_github_actions_json, required_skills_json, confidence, created_at)
                VALUES ('route-pr-2886', ?, 'new-pr', 'new_pr', '["push_existing_pr", "open_pr", "comment"]', '["fast-small-pr"]', 'high', ?)
                """,
                (task_id, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at, metadata_json)
                VALUES (?, ?, 1, 'prepared', ?, 'codex/issue-2884-fix-operator', ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (
                    attempt_id,
                    task_id,
                    str(self.worktree_root / "issue-2884-fix-operator"),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at)
                VALUES ('artifact-pr-2886', ?, ?, 'prompt', ?, ?)
                """,
                (task_id, attempt_id, str(prompt_path), now),
            )

        pr_fixture = self.root / "pr-2886-followup.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-2886-followup",
                            "number": 2886,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot handle the major review items",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2884-fix-operator",
                            "existing_pr_head_sha": "rerouted-head-sha",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#2884",
                                    "origin_workstream_id": "github:example/backend#2884",
                                    "source_issue": "2884",
                                }
                            },
                            "workstream_id": "github:example/backend!2886",
                            "origin_workstream_id": "github:example/backend#2884",
                            "event_at": "2026-06-17T09:53:31Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT t.route_id, t.expected_output, a.branch_name,
                       a.metadata_json
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                WHERE t.workstream_id = 'github:example/backend!2886'
                """
            ).fetchone()
            prompt_path = conn.execute(
                """
                SELECT path
                FROM artifacts
                WHERE task_id = ? AND artifact_type = 'prompt'
                """,
                (task_id,),
            ).fetchone()[0]
            relationships = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.relationship, ge.event_fingerprint
                """,
                (task_id,),
            ).fetchall()
        if blocked:
            return
        self.assertEqual(
            row[:3],
            (
                "update-existing-pr",
                "update_existing_pr",
                "codex/issue-2884-fix-operator",
            ),
        )
        self.assertEqual(
            json.loads(row[3])["existing_pr_head_sha"],
            "rerouted-head-sha",
        )
        prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.assertIn("expected_output: update_existing_pr", prompt)
        self.assertIn("push_existing_pr", prompt)
        self.assertIn("comment:pr-2886-followup", prompt)
        self.assertEqual(
            relationships,
            [("context", "comment:pr-2886-followup")],
        )

    def test_live_dispatch_uses_refreshed_worktree_after_prepared_pr_reroute(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.persistence.schema import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        task_id = "task-pr-2886"
        attempt_id = "attempt-pr-2886"
        prompt_path = self.data_dir / "artifacts" / task_id / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("stale prompt", encoding="utf-8")
        old_worktree_path = self.worktree_root / "dd-2884-task"
        new_worktree_path = self.worktree_root / "codex__issue-2884-fix-operator"
        now = "2026-06-17T09:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "repo:example/backend",
                    "example/backend",
                    "robert-bot",
                    "master",
                    str(self.repo_root),
                    str(self.worktree_root),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend!2886', 'repo:example/backend', 'github:example/backend!2886', 'pull_request', 2886, 'fix(operator): improve release log context', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend!2886', 'repo:example/backend', 'source:github:example/backend!2886', 'github:example/backend#2884', 'active', ?, ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES (?, 'github:example/backend!2886', 'queued', 'P1', 'new-pr', 'new_pr', ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO route_decisions(route_decision_id, task_id, route_id, expected_output, allowed_github_actions_json, required_skills_json, confidence, created_at)
                VALUES ('route-pr-2886', ?, 'new-pr', 'new_pr', '["push_existing_pr", "open_pr", "comment"]', '["fast-small-pr"]', 'high', ?)
                """,
                (task_id, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at, metadata_json)
                VALUES (?, ?, 1, 'prepared', ?, 'codex/dd-2884-task', ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (
                    attempt_id,
                    task_id,
                    str(old_worktree_path),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at)
                VALUES ('artifact-pr-2886', ?, ?, 'prompt', ?, ?)
                """,
                (task_id, attempt_id, str(prompt_path), now),
            )

        pr_fixture = self.root / "pr-2886-live-followup.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-2886-followup",
                            "number": 2886,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot handle the major review items",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2884-fix-operator",
                            "metadata": {
                                "dd_workstream": {
                                    "origin_workstream_id": "github:example/backend#2884",
                                    "source_issue": "2884",
                                }
                            },
                            "workstream_id": "github:example/backend!2886",
                            "origin_workstream_id": "github:example/backend#2884",
                            "event_at": "2026-06-17T09:53:31Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        dispatch_calls = []
        prepare_calls = []
        original_dispatch_worker = cycle_worker.dispatch_worker
        original_prepare_worktree = assigned_workspace.prepare_worktree

        def fake_prepare_worktree(repo, event, route_result, dry_run):
            prepare_calls.append(dry_run)
            self.assertFalse(dry_run)
            return {
                "worktree_path": str(new_worktree_path),
                "branch_name": "codex/issue-2884-fix-operator",
            }

        def fake_dispatch_worker(**kwargs):
            dispatch_calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 456, "command": ["cbc"]}

        try:
            assigned_workspace.prepare_worktree = fake_prepare_worktree
            cycle_worker.dispatch_worker = fake_dispatch_worker
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=pr_fixture,
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch_worker
            assigned_workspace.prepare_worktree = original_prepare_worktree

        self.assertTrue(result["ok"], result)
        self.assertEqual(prepare_calls, [False, False])
        self.assertEqual(len(dispatch_calls), 1)
        self.assertEqual(dispatch_calls[0]["worktree_path"], str(new_worktree_path))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt = conn.execute(
                """
                SELECT worktree_path, branch_name
                FROM attempts
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt, (str(new_worktree_path), "codex/issue-2884-fix-operator"))

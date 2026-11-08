from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _Completed,
    _verification_evidence,
)


class FollowupIntakeTests(RepositoryCycleFixture):
    def test_existing_workstream_followup_is_context_not_new_task(self):
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "@robert-bot this extra detail may matter",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            relationships = {
                row[0] for row in conn.execute("SELECT relationship FROM task_events ORDER BY relationship")
            }
        self.assertEqual(task_count, 1)
        self.assertEqual(relationships, {"context", "trigger"})
        with closing(sqlite3.connect(db_path)) as conn:
            event = conn.execute(
                "SELECT authorization_status FROM github_events WHERE event_fingerprint='comment:comment-2'"
            ).fetchone()
        self.assertEqual(event, ("context_only",))

    def test_existing_workstream_followup_without_mention_is_ignored(self):
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "this extra detail may matter",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            relationships = {
                row[0] for row in conn.execute("SELECT relationship FROM task_events ORDER BY relationship")
            }
            event_status = conn.execute(
                """
                SELECT authorization_status
                FROM github_events
                WHERE event_fingerprint = 'comment:comment-2'
                """
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(relationships, {"trigger"})
        self.assertEqual(event_status, "ignored_context")

    def test_active_issue_workstream_does_not_capture_pr_followup_task(self):
        from robert_agent.runtime import cycle as run_once

        issue_fixture = self.root / "issue.json"
        issue_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "issue-comment-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this",
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pr_fixture = self.root / "pr.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-comment-1",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot follow up",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-123-fix",
                            "metadata": {
                                "dd_workstream": {
                                    "origin_workstream_id": "github:example/backend#123",
                                    "source_issue": "123",
                                }
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=issue_fixture,
            dry_run=True,
            skip_external=True,
        )
        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            workstream_ids = [row[0] for row in conn.execute("SELECT workstream_id FROM tasks ORDER BY created_at")]
        self.assertEqual(
            workstream_ids,
            [
                "github:example/backend#123",
                "github:example/backend!456",
            ],
        )

    def test_member_context_on_known_inactive_pr_workstream_does_not_create_task(self):
        from robert_agent.runtime import cycle as run_once

        db_path = self.data_dir / "dd.sqlite3"
        initial = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(initial["ok"], initial)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, state, author_login
                )
                VALUES (?, ?, ?, 'pull_request', 456, '', 'open', 'robert-bot')
                """,
                (
                    "source:github:example/backend!456",
                    "repo:example/backend",
                    "github:example/backend!456",
                ),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, ?, ?, 'completed', NULL, ?, ?)
                """,
                (
                    "github:example/backend!456",
                    "repo:example/backend",
                    "source:github:example/backend!456",
                    now,
                    now,
                ),
            )

        pr_fixture = self.root / "inactive-known-pr.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-member-followup",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "@robert-bot follow-up detail, please fix this bug",
                            "workstream_id": "github:example/backend!456",
                            "has_open_dd_pr": True,
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
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            pr_task_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE workstream_id = 'github:example/backend!456'
                """
            ).fetchone()[0]
            event_row = conn.execute(
                """
                SELECT authorization_status, actor_login, event_fingerprint
                FROM github_events
                WHERE event_fingerprint = 'comment:pr-member-followup'
                """
            ).fetchone()
        self.assertEqual(pr_task_count, 0)
        self.assertEqual(event_row, ("context_only", "reviewer", "comment:pr-member-followup"))

    def test_trusted_context_on_known_inactive_pr_workstream_creates_new_task(self):
        from robert_agent.runtime import cycle as run_once

        db_path = self.data_dir / "dd.sqlite3"
        initial = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(initial["ok"], initial)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, state, author_login
                )
                VALUES (?, ?, ?, 'pull_request', 456, '', 'open', 'robert-bot')
                """,
                (
                    "source:github:example/backend!456",
                    "repo:example/backend",
                    "github:example/backend!456",
                ),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, ?, ?, 'completed', NULL, ?, ?)
                """,
                (
                    "github:example/backend!456",
                    "repo:example/backend",
                    "source:github:example/backend!456",
                    now,
                    now,
                ),
            )

        pr_fixture = self.root / "inactive-known-pr-trusted.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-trusted-followup",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "OWNER",
                            "body": "@robert-bot please analyze this follow-up",
                            "workstream_id": "github:example/backend!456",
                            "has_open_dd_pr": True,
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT workstream_id, lifecycle
                FROM tasks
                WHERE workstream_id = 'github:example/backend!456'
                """
            ).fetchall()
        self.assertEqual(rows, [("github:example/backend!456", "queued")])

    def test_issue_to_pr_review_followup_keeps_workstreams_separate(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

        def fake_issue_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"] and "--assignee" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "search", "issues"] and "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-issue-123",
                          "updated_at": "2026-06-22T12:00:00Z",
                          "reason": "mention",
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/123"
                          },
                          "repository": {
                            "full_name": "example/backend"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(
                    args,
                    """{
                      "number": 123,
                      "state": "open",
                      "title": "Compatibility bug",
                      "updated_at": "2026-06-22T12:00:00Z",
                      "html_url": "https://github.com/example/backend/issues/123",
                      "user": {"login": "wklken"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-1",
                        "body": "@robert-bot please fix this bug",
                        "created_at": "2026-06-22T12:00:01Z",
                        "author_association": "OWNER",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        issue_run = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_issue_runner,
        )
        self.assertTrue(issue_run["ok"], issue_run)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            issue_task_id, issue_attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()

        record = result.record_result(
            db_path,
            {
                "task_id": issue_task_id,
                "attempt_id": issue_attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        task_id=issue_task_id,
                        issue_number="123",
                        pr_number="456",
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "opened PR 456 for issue 123",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )

            conn.execute(
                "UPDATE github_actions SET target_url='https://github.com/example/backend/pull/456', external_id='456' WHERE action_type='open_pr'"
            )

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(finalized["ok"], finalized)

        def fake_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(args, '{"number":123,"state":"open"}')
            if args[:3] == ["gh", "search", "issues"] and "--assignee" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "search", "issues"] and "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-pr-456",
                          "updated_at": "2026-06-22T13:00:00Z",
                          "reason": "comment",
                          "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/example/backend/pulls/456"
                          },
                          "repository": {
                            "full_name": "example/backend"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456"]:
                return _Completed(
                    args,
                    """{
                      "number": 456,
                      "state": "open",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/456",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456"]:
                return _Completed(
                    args,
                    f"""{{
                      "body": "<!-- robert-workstream\\norigin_workstream_id: github:example/backend#123\\nsource_issue: 123\\ntask_id: {issue_task_id}\\ncreated_by: robert\\n-->\\nOpened PR",
                      "head": {{"ref": "codex/dd-123-task"}},
                      "user": {{"login": "robert-bot"}}
                    }}""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456/reviews"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "review-comment-456",
                        "body": "@robert-bot please fix the compatibility path on this branch.",
                        "created_at": "2026-06-22T13:00:01Z",
                        "author_association": "MEMBER",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        pr_followup = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_pr_runner,
        )

        self.assertTrue(pr_followup["ok"], pr_followup)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            workstreams = conn.execute(
                """
                SELECT workstream_id, origin_workstream_id, lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id IN (
                  'github:example/backend#123',
                  'github:example/backend!456'
                )
                ORDER BY workstream_id
                """
            ).fetchall()
            tasks = conn.execute(
                """
                SELECT t.task_id, t.workstream_id, t.parent_task_id, t.route_id,
                       t.expected_output, a.branch_name
                FROM tasks t
                LEFT JOIN attempts a ON a.task_id = t.task_id
                ORDER BY t.created_at, t.task_id
                """
            ).fetchall()
            event_relationships = conn.execute(
                """
                SELECT t.workstream_id, te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN tasks t ON t.task_id = te.task_id
                JOIN github_events ge ON ge.event_id = te.event_id
                ORDER BY t.workstream_id, ge.event_fingerprint
                """
            ).fetchall()

        self.assertEqual(
            workstreams,
            [
                (
                    "github:example/backend!456",
                    "github:example/backend#123",
                    "active",
                    tasks[1][0],
                ),
                (
                    "github:example/backend#123",
                    None,
                    "completed",
                    None,
                ),
            ],
        )
        self.assertEqual(tasks[0][1], "github:example/backend#123")
        self.assertIsNone(tasks[0][2])
        self.assertEqual(tasks[0][3:5], ("new-pr", "new_pr"))
        self.assertEqual(tasks[1][1], "github:example/backend!456")
        self.assertIsNone(tasks[1][2])
        self.assertEqual(
            tasks[1][3:6],
            ("update-existing-pr", "update_existing_pr", "codex/dd-123-task"),
        )
        self.assertEqual(
            event_relationships,
            [
                (
                    "github:example/backend!456",
                    "trigger",
                    "review_comment:review-comment-456",
                ),
                (
                    "github:example/backend#123",
                    "consumed",
                    "comment:comment-1",
                ),
            ],
        )

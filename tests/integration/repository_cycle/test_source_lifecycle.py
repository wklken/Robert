from robert_agent.adapters.processes import attempt as attempt_process
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from tests.support import PACKAGE_ROOT
from robert_agent.work.application import source_events
from robert_agent.work.application import source_lifecycle
from tests.support.repository_cycle import (
    RepositoryCycleFixture, _Completed,
)


class SourceLifecycleTests(RepositoryCycleFixture):
    def test_event_without_state_preserves_a_closed_source(self):
        from robert_agent.runtime import cycle as run_once

        self.assertTrue(run_once.run_once(
            self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True,
        )["ok"])
        db = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE github_sources SET state='closed' WHERE source_key='github:example/backend#123'")
            event = {
                "source_key": "github:example/backend#123", "source_type": "issue",
                "number": 123, "event_fingerprint": "comment:missing-state",
                "event_type": "comment", "actor_login": "wklken",
                "event_at": "2026-07-31T10:00:00Z",
            }
            source_events.insert_source_and_event(conn, "repo:example/backend", event, event["event_at"])
            self.assertEqual(conn.execute("SELECT state FROM github_sources").fetchone()[0], "closed")
            event.update(state="open", event_fingerprint="comment:explicit-reopen")
            source_events.insert_source_and_event(conn, "repo:example/backend", event, event["event_at"])
            self.assertEqual(conn.execute("SELECT state FROM github_sources").fetchone()[0], "open")

    def test_remote_close_reconciles_an_unfinished_historical_issue_item(self):
        from robert_agent.readmodels import board
        from robert_agent.runtime import cycle as run_once
        from robert_agent.work.application import commands as work_items

        for state_reason in ("completed", "not_planned"):
            with self.subTest(state_reason=state_reason):
                fixture = RepositoryCycleFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                self.assertTrue(run_once.run_once(
                    fixture.config_path, fixture_path=fixture.fixture_path, dry_run=True, skip_external=True,
                )["ok"])
                db = fixture.data_dir / "dd.sqlite3"
                with closing(sqlite3.connect(db)) as conn, conn:
                    item = work_items._item_from_row(conn.execute("SELECT * FROM work_items").fetchone())
                    conn.execute("UPDATE work_items SET completed_at=NULL, canceled_at=NULL")
                    conn.execute("UPDATE tasks SET lifecycle='completed'")
                    conn.execute("UPDATE attempts SET status='completed'")
                    conn.execute("UPDATE workstreams SET lifecycle='completed', active_task_id=NULL")
                    work_items.record_system_event(
                        conn, item["work_item_id"], event_type="operator_decision_required",
                        idempotency_key="historical-approval:" + state_reason,
                    )
                    remote = {
                        "state": "closed", "state_reason": state_reason,
                        "closed_at": "2026-07-31T09:45:22Z",
                        "updated_at": "2026-07-31T09:45:22Z",
                        "title": "Completed issue",
                    }
                    calls = []

                    def runner(args, **_kwargs):
                        calls.append(args)
                        self.assertEqual(args, ["gh", "api", "repos/example/backend/issues/123"])
                        return _Completed(args, json.dumps(remote))

                    reconciled = source_lifecycle.reconcile_remote_source_states(
                        conn, "repo:example/backend", {"full_name": "example/backend"},
                        runner, "2026-10-27T00:00:00+00:00",
                    )
                    self.assertEqual(reconciled, 1)
                    self.assertEqual(len(calls), 1)
                detail = board.get_work_item_detail(db, item["work_item_id"])
                self.assertEqual(detail["column"], "done" if state_reason == "completed" else "history")
                self.assertEqual(detail["attention_signals"], [])
                self.assertNotIn("approve", detail["valid_commands"])
                self.assertEqual(detail["completed_at"] if state_reason == "completed" else detail["canceled_at"], remote["closed_at"])
                version = detail["version"]
                with closing(sqlite3.connect(db)) as conn, conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
                    self.assertEqual(source_lifecycle.reconcile_remote_source_states(
                        conn, "repo:example/backend", {"full_name": "example/backend"},
                        runner, "2026-10-27T00:05:00+00:00",
                    ), 0)
                self.assertEqual(board.get_work_item_detail(db, item["work_item_id"])["version"], version)

    def test_remote_completed_issue_marks_its_item_done(self):
        from robert_agent.readmodels import board
        from robert_agent.runtime import cycle as run_once

        self.assertTrue(run_once.run_once(
            self.config_path, fixture_path=self.fixture_path, dry_run=True, skip_external=True,
        )["ok"])
        db = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            item = dict(conn.execute("SELECT * FROM work_items").fetchone())
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            remote = {"state": "closed", "state_reason": "completed", "closed_at": "2026-07-31T09:45:22Z"}
            self.assertTrue(source_lifecycle.record_remote_terminal_state(
                conn, item["repo_id"], item["workstream_id"], item["origin_source_id"],
                "github:example/backend#123", "issue", 123, task_id, remote,
                "completed", "remote_source_closed", "2026-10-27T00:00:00+00:00",
            ))
        detail = board.get_work_item_detail(db, item["work_item_id"])
        self.assertEqual(detail["column"], "done")
        self.assertEqual(detail["completed_at"], remote["closed_at"])
        self.assertEqual(detail["valid_commands"], [])

    def test_active_issue_task_is_canceled_when_remote_issue_closes(self):
        from robert_agent.runtime import cycle as run_once
        def fake_open_issue_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
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

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_open_issue_runner,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

        def fake_closed_issue_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(
                    args,
                    """{
                      "number": 123,
                      "state": "closed",
                      "state_reason": "not_planned",
                      "title": "Compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/issues/123",
                      "user": {"login": "wklken"}
                    }""",
                )
            raise AssertionError(args)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_closed_issue_runner,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            workstream = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend#123'
                """
            ).fetchone()
            source = conn.execute(
                """
                SELECT state, metadata_json
                FROM github_sources
                WHERE source_key = 'github:example/backend#123'
                """
            ).fetchone()
            audit = conn.execute(
                """
                SELECT event_type, payload_json
                FROM audit_events
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(task, ("canceled",))
        self.assertEqual(attempt, ("canceled",))
        self.assertEqual(workstream, ("canceled", None))
        self.assertEqual(source[0], "closed")
        self.assertEqual(json.loads(source[1])["remote_state"]["state_reason"], "not_planned")
        self.assertEqual(audit[0], "remote_source_closed")
        self.assertEqual(json.loads(audit[1])["source_key"], "github:example/backend#123")

    def test_active_pr_task_is_canceled_when_remote_pr_is_merged(self):
        from robert_agent.runtime import cycle as run_once
        db_path, task_id, attempt_id = self._seed_active_pr_task(456)

        def fake_merged_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456"]:
                return _Completed(
                    args,
                    """{
                      "number": 456,
                      "state": "closed",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/456",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456"]:
                return _Completed(
                    args,
                    """{
                      "number": 456,
                      "state": "closed",
                      "merged": true,
                      "merged_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/456"
                    }""",
                )
            raise AssertionError(args)

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_merged_pr_runner,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            workstream = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!456'
                """
            ).fetchone()
            source_metadata = json.loads(
                conn.execute(
                    """
                    SELECT metadata_json
                    FROM github_sources
                    WHERE source_key = 'github:example/backend!456'
                    """
                ).fetchone()[0]
            )
            audit = conn.execute(
                """
                SELECT event_type, payload_json
                FROM audit_events
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(task, ("canceled",))
        self.assertEqual(attempt, ("canceled",))
        self.assertEqual(workstream, ("completed", None))
        self.assertTrue(source_metadata["remote_state"]["merged"])
        self.assertEqual(audit[0], "remote_pr_merged")
        self.assertEqual(json.loads(audit[1])["source_key"], "github:example/backend!456")

    def test_completed_pr_workstream_marks_review_item_done_when_remote_pr_is_merged(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.readmodels import board
        from robert_agent.work.application import commands as work_items

        db_path, task_id, attempt_id = self._seed_active_pr_task(459)
        now = "2026-06-22T12:00:00+00:00"
        issue_workstream_id = "github:example/backend#123"
        pr_workstream_id = "github:example/backend!459"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  html_url, title, state, author_login
                )
                VALUES (
                  'source:github:example/backend#123',
                  'repo:example/backend',
                  ?, 'issue', 123,
                  'https://github.com/example/backend/issues/123',
                  'Compatibility bug', 'open', 'wklken'
                )
                """,
                (issue_workstream_id,),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  active_task_id, created_at, updated_at
                )
                VALUES (
                  ?, 'repo:example/backend',
                  'source:github:example/backend#123',
                  'completed', NULL, ?, ?
                )
                """,
                (issue_workstream_id, now, now),
            )
            item = work_items.ensure_github_work_item(
                conn,
                repo_id="repo:example/backend",
                source_id="source:github:example/backend#123",
                workstream_id=issue_workstream_id,
                actor_identity="wklken",
                route_confidence="high",
                now=now,
            )
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="pr_opened",
                idempotency_key="pr-opened:459",
                metadata={"source_key": pr_workstream_id, "pr_number": 459},
                now=now,
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            conn.execute(
                "UPDATE attempts SET status = 'completed', finished_at = ? WHERE attempt_id = ?",
                (now, attempt_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'completed', active_task_id = NULL, updated_at = ?
                WHERE workstream_id = ?
                """,
                (now, pr_workstream_id),
            )

        self.assertEqual(
            board.get_work_item_detail(db_path, item["work_item_id"])["column"],
            "review",
        )

        def fake_merged_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(args, '{"number":123,"state":"open"}')
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/459"]:
                return _Completed(
                    args,
                    """{
                      "number": 459,
                      "state": "closed",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/459",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/459"]:
                return _Completed(
                    args,
                    """{
                      "number": 459,
                      "state": "closed",
                      "merged": true,
                      "merged_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/459"
                    }""",
                )
            raise AssertionError(args)

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_merged_pr_runner,
        )

        self.assertTrue(result["ok"], result)
        detail = board.get_work_item_detail(db_path, item["work_item_id"])
        self.assertEqual(detail["column"], "done")
        self.assertIn("pr_merged", [event["event_type"] for event in detail["events"]])

    def test_active_pr_task_is_canceled_when_remote_pr_is_closed_unmerged(self):
        from robert_agent.runtime import cycle as run_once
        db_path, task_id, attempt_id = self._seed_active_pr_task(457)

        def fake_closed_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/457"]:
                return _Completed(
                    args,
                    """{
                      "number": 457,
                      "state": "closed",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/457",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/457"]:
                return _Completed(
                    args,
                    """{
                      "number": 457,
                      "state": "closed",
                      "merged": false,
                      "merged_at": null,
                      "html_url": "https://github.com/example/backend/pull/457"
                    }""",
                )
            raise AssertionError(args)

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_closed_pr_runner,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            workstream = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!457'
                """
            ).fetchone()
            source_metadata = json.loads(
                conn.execute(
                    """
                    SELECT metadata_json
                    FROM github_sources
                    WHERE source_key = 'github:example/backend!457'
                    """
                ).fetchone()[0]
            )
            audit = conn.execute(
                """
                SELECT event_type, payload_json
                FROM audit_events
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(task, ("canceled",))
        self.assertEqual(attempt, ("canceled",))
        self.assertEqual(workstream, ("canceled", None))
        self.assertFalse(source_metadata["remote_state"]["merged"])
        self.assertEqual(audit[0], "remote_pr_closed")
        self.assertEqual(json.loads(audit[1])["source_key"], "github:example/backend!457")

    def test_closed_issue_source_is_skipped_before_dispatch(self):
        from robert_agent.runtime import cycle as run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "closed-issue-comment",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please handle this closed issue",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/issues/123",
                            "state": "closed",
                            "state_reason": "completed",
                            "closed_at": "2026-06-22T13:00:00Z",
                            "source_updated_at": "2026-06-22T13:00:00Z",
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
        self.assertEqual(result["prompt_paths"], [])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            attempt_count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            source_state, source_metadata_json = conn.execute(
                """
                SELECT state, metadata_json
                FROM github_sources
                WHERE source_key = 'github:example/backend#123'
                """
            ).fetchone()
            event_status = conn.execute(
                """
                SELECT authorization_status
                FROM github_events
                WHERE event_fingerprint = 'comment:closed-issue-comment'
                """
            ).fetchone()[0]
            audit = conn.execute(
                "SELECT event_type, payload_json FROM audit_events"
            ).fetchone()
            route_output = json.loads(
                conn.execute(
                    """
                    SELECT output_json
                    FROM run_steps
                    WHERE step_key = 'route'
                    """
                ).fetchone()[0]
            )
        source_metadata = json.loads(source_metadata_json)
        audit_payload = json.loads(audit[1])
        self.assertEqual(task_count, 0)
        self.assertEqual(attempt_count, 0)
        self.assertEqual(source_state, "closed")
        self.assertEqual(event_status, "ignored_current_state_closed")
        self.assertEqual(audit[0], "current_state_closed")
        self.assertEqual(audit_payload["terminal_reason"], "remote_source_closed")
        self.assertEqual(audit_payload["skip_reason"], "source_already_closed_before_dispatch")
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["terminal_reason"],
            "remote_source_closed",
        )
        self.assertEqual(route_output["current_state_closed_skip_count"], 1)

    def test_merged_pr_source_is_skipped_before_dispatch(self):
        from robert_agent.runtime import cycle as run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "merged-pr-comment",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please handle this merged PR",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/pull/456",
                            "state": "closed",
                            "merged": True,
                            "merged_at": "2026-06-22T13:00:00Z",
                            "closed_at": "2026-06-22T13:00:00Z",
                            "source_updated_at": "2026-06-22T13:00:00Z",
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
        self.assertEqual(result["prompt_paths"], [])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            source_metadata = json.loads(
                conn.execute(
                    """
                    SELECT metadata_json
                    FROM github_sources
                    WHERE source_key = 'github:example/backend!456'
                    """
                ).fetchone()[0]
            )
            audit_payload = json.loads(
                conn.execute("SELECT payload_json FROM audit_events").fetchone()[0]
            )
        self.assertEqual(task_count, 0)
        self.assertEqual(audit_payload["terminal_reason"], "remote_pr_merged")
        self.assertTrue(audit_payload["remote_state"]["merged"])
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["terminal_reason"],
            "remote_pr_merged",
        )

    def test_closed_pr_followup_for_active_workstream_is_not_skipped(self):
        from robert_agent.runtime import cycle as run_once
        db_path, task_id, _attempt_id = self._seed_active_pr_task(458)
        followup_fixture = self.root / "closed-pr-followup.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "closed-pr-followup",
                            "number": 458,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot handle the explicit follow-up",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/pull/458",
                            "state": "closed",
                            "merged": True,
                            "merged_at": "2026-06-22T13:00:00Z",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/dd-458-task",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#123",
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

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            relationships = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.created_at, ge.event_fingerprint
                """,
                (task_id,),
            ).fetchall()
            current_state_audits = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'current_state_closed'"
            ).fetchone()[0]
        self.assertIn(("context", "comment:closed-pr-followup"), relationships)
        self.assertEqual(current_state_audits, 0)

    def test_remote_close_waits_for_process_cleanup_and_preserves_dispatch_metadata(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        run_once.run_once(self.config_path, fixture_path=self.fixture_path,
                          dry_run=True, skip_external=True)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_id, attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
            conn.execute("UPDATE tasks SET lifecycle = 'running'")
            conn.execute("UPDATE attempts SET status = 'running', metadata_json = json_set(?, '$.result_contract', 'compact-v1')",
                         (json.dumps({"dispatch": {"pid": 12345}}),))
            repo_id, ws, source_id, source_key, source_type, number = conn.execute(
                "SELECT w.repo_id,w.workstream_id,gs.source_id,gs.source_key,gs.source_type,gs.number "
                "FROM workstreams w JOIN github_sources gs ON gs.source_id=w.primary_source_id"
            ).fetchone()
            args = (conn, repo_id, ws, source_id, source_key, source_type, number,
                    task_id, {"state": "closed"}, "canceled", "remote_issue_closed", datetime.now(timezone.utc).isoformat())
            conn.execute("UPDATE attempts SET metadata_json = json_set('{}', '$.result_contract', 'compact-v1')")
            self.assertFalse(source_lifecycle.record_remote_terminal_state(*args))
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")
            conn.execute("UPDATE attempts SET metadata_json = json_set(?, '$.result_contract', 'compact-v1')", (json.dumps({"dispatch": {"pid": 12345}}),))
            with patch.object(attempt_process, "terminate_attempt", return_value={"status": "permission_denied"}):
                source_lifecycle.record_remote_terminal_state(*args)
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")
            self.assertEqual(conn.execute("SELECT active_task_id FROM workstreams").fetchone()[0], task_id)
            with patch.object(attempt_process, "terminate_attempt", return_value={"status": "terminated"}):
                source_lifecycle.record_remote_terminal_state(*args)
            status, metadata = conn.execute("SELECT status,metadata_json FROM attempts").fetchone()
            self.assertEqual(status, "canceled")
            self.assertEqual(json.loads(metadata)["dispatch"]["pid"], 12345)

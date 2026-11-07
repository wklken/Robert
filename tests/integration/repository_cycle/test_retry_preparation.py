from contextlib import closing
import json
import sqlite3
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class RetryPreparationTests(RepositoryCycleFixture):
    def test_retry_after_failed_task_includes_original_trigger_as_context(self):
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
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle = 'failed' WHERE task_id = ?", (task_id,))
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE attempt_id = ?",
                ("2026-06-23T04:00:00+00:00", attempt_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL
                """
            )

        followup_fixture = self.root / "followup.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please also add tests",
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
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute("SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at").fetchall()
            child_events = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.relationship, ge.event_at
                """,
                (tasks[1][0],),
            ).fetchall()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1][1], task_id)
        fingerprints = {row[1] for row in child_events}
        self.assertIn("comment:comment-1", fingerprints)
        self.assertIn("comment:comment-2", fingerprints)
        trigger_row = [row for row in child_events if row[0] == "trigger"]
        self.assertEqual(len(trigger_row), 1)

    def test_retry_after_failed_task_keeps_original_trigger_primary(self):
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
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute("UPDATE tasks SET lifecycle = 'failed' WHERE task_id = ?", (task_id,))
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE attempt_id = ?",
                ("2026-06-23T04:00:00+00:00", attempt_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL
                """
            )

        followup_fixture = self.root / "followup-mentioned.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please also add tests",
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
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute("SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at").fetchall()
            child_events = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY CASE te.relationship WHEN 'trigger' THEN 0 ELSE 1 END, ge.event_at
                """,
                (tasks[1][0],),
            ).fetchall()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1][1], task_id)
        self.assertEqual(child_events[0], ("trigger", "comment:comment-1"))
        self.assertIn(("context", "comment:comment-2"), child_events)

    def test_manual_retrigger_prepares_child_from_failed_github_task(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.work.application import commands as work_items
        from robert_agent.execution import ownership as execution_ownership

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
            parent_task_id, workstream_id, parent_route, parent_output = conn.execute(
                """
                SELECT task_id, workstream_id, route_id, expected_output
                FROM tasks ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed' WHERE task_id = ?",
                (parent_task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE task_id = ?",
                ("2026-10-19T00:01:00+00:00", parent_task_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL
                WHERE workstream_id = ?
                """,
                (workstream_id,),
            )
            # This fixture was only prepared in dry-run; no process was launched.
            conn.execute(
                "UPDATE attempts SET metadata_json=json_remove(metadata_json,'$.dispatch') WHERE task_id=?",
                (parent_task_id,),
            )
            execution_ownership.close_terminal_task(conn, parent_task_id, "2026-10-19T00:01:00+00:00")
        context = work_items.CommandContext(
            actor_kind="operator",
            actor_identity="owner",
            allowed_repo_ids=frozenset({"repo:example/backend"}),
            allowed_workers=frozenset({"default"}),
        )
        queued = work_items.retrigger_failed_github_task(
            db_path,
            parent_task_id,
            context=context,
            idempotency_key="manual-retrigger-1",
            notifier=lambda _path: None,
        )
        self.assertEqual(queued["status"], "queued")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            self.assertFalse(execution_ownership.parent_allows_child(conn, parent_task_id))
            self.assertFalse(
                execution_ownership.parent_allows_child(conn, parent_task_id, retrigger_wakeup_id="unrelated")
            )
            self.assertTrue(
                execution_ownership.parent_allows_child(conn, parent_task_id, retrigger_wakeup_id=queued["wakeup_id"])
            )
            self.assertTrue(
                execution_ownership.parent_allows_child(conn, parent_task_id, retrigger_wakeup_id=queued["wakeup_id"])
            )
            conn.execute(
                "UPDATE execution_subjects SET binding_status='attention' WHERE subject_id=?",
                (parent_task_id,),
            )
            self.assertFalse(
                execution_ownership.parent_allows_child(conn, parent_task_id, retrigger_wakeup_id=queued["wakeup_id"])
            )
            conn.execute(
                "UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?",
                (parent_task_id,),
            )
        followup_fixture = self.root / "retrigger-followup.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-retrigger-followup",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot include the latest acceptance note",
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn:
            tasks = conn.execute(
                """
                SELECT task_id, parent_task_id, lifecycle, route_id, expected_output
                FROM tasks ORDER BY created_at, task_id
                """
            ).fetchall()
            child_task_id = tasks[-1][0]
            parent_events = conn.execute(
                """
                SELECT relationship, event_id FROM task_events
                WHERE task_id = ? ORDER BY relationship, event_id
                """,
                (parent_task_id,),
            ).fetchall()
            child_events = conn.execute(
                """
                SELECT relationship, event_id FROM task_events
                WHERE task_id = ? ORDER BY relationship, event_id
                """,
                (child_task_id,),
            ).fetchall()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE task_id = ?",
                (child_task_id,),
            ).fetchone()
            wakeup_status = conn.execute(
                """
                SELECT status FROM wakeups
                WHERE task_id = ? AND reason = 'manual_operator_request'
                """,
                (parent_task_id,),
            ).fetchone()[0]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            tasks[-1][1:],
            (parent_task_id, "queued", parent_route, parent_output),
        )
        self.assertIn(parent_events[0], child_events)
        self.assertIn(("context", "event:comment:comment-retrigger-followup"), child_events)
        self.assertEqual(attempt[0], "prepared")
        self.assertEqual(wakeup_status, "consumed")

from contextlib import closing
import json
import sqlite3
from tests.support import PACKAGE_ROOT
from robert_agent.work.application import source_events
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class EventConsumptionTests(RepositoryCycleFixture):
    def test_dry_run_respects_max_dispatches(self):
        from robert_agent.runtime import cycle as run_once

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this bug",
                            "intent": "bug_fix",
                        },
                        {
                            "id": "comment-2",
                            "number": 124,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please add tests",
                            "intent": "add_tests",
                        },
                        {
                            "id": "comment-3",
                            "number": 125,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please make a small change",
                            "intent": "small_change",
                        },
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
            max_dispatches=1,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["dispatch_count"], 1)
        self.assertEqual(len(result["prompt_paths"]), 3)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            dispatched_attempt_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM attempts
                WHERE json_extract(metadata_json, '$.dispatch.status') = 'prepared'
                """
            ).fetchone()[0]
        self.assertEqual(task_count, 3)
        self.assertEqual(dispatched_attempt_count, 1)

    def test_second_run_preserves_existing_control_plane_rows(self):
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            first_task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            first_attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_rows = conn.execute("SELECT task_id FROM tasks").fetchall()
            attempt_rows = conn.execute("SELECT attempt_id FROM attempts").fetchall()
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            event_status = conn.execute(
                """
                SELECT authorization_status
                FROM github_events
                WHERE event_fingerprint = 'comment:comment-1'
                """
            ).fetchone()[0]
        self.assertIn((first_task_id,), task_rows)
        self.assertIn((first_attempt_id,), attempt_rows)
        self.assertEqual(len(task_rows), 1)
        self.assertEqual(len(attempt_rows), 1)
        self.assertEqual(result_count, 0)
        self.assertEqual(event_status, "authorized_trigger")

    def test_completed_consumed_trigger_is_not_reprocessed(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once

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
                            "body": "@robert-bot please look at this",
                            "intent": "unclear",
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
                    "consumed_event_fingerprints": ["comment:unclear-1"],
                    "verification": [],
                    "handoff": "No action needed",
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

        replay = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(replay["ok"], replay)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            relationship = conn.execute(
                "SELECT relationship FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        # The classifier completes with one clarification child; replay must not
        # interpret its consumed trigger as another request.
        self.assertEqual(task_count, 2)
        self.assertEqual(relationship, "consumed")

    def test_consumed_comment_is_deduplicated_independently_of_task_lifecycle(self):

        with closing(sqlite3.connect(":memory:")) as conn:
            conn.executescript("""
                CREATE TABLE tasks(task_id TEXT, lifecycle TEXT);
                CREATE TABLE github_events(event_id TEXT, event_fingerprint TEXT);
                CREATE TABLE task_events(task_id TEXT, event_id TEXT, relationship TEXT);
                INSERT INTO tasks VALUES ('task-1', 'waiting_for_user');
                INSERT INTO github_events VALUES ('event-1', 'comment:1');
                INSERT INTO task_events VALUES ('task-1', 'event-1', 'consumed');
            """)
            event = {"event_fingerprint": "comment:1", "actor_kind": "github_user"}
            for lifecycle, relationship, expected in (
                ("waiting_for_user", "consumed", True),
                ("running", "consumed", True),
                ("completed", "consumed", True),
                ("waiting_for_user", "trigger", False),
                ("completed", "trigger", True),
                ("waiting_for_user", "context", False),
            ):
                with self.subTest(lifecycle=lifecycle, relationship=relationship):
                    conn.execute("UPDATE tasks SET lifecycle = ?", (lifecycle,))
                    conn.execute("UPDATE task_events SET relationship = ?", (relationship,))
                    self.assertEqual(
                        source_events.should_skip_terminal_trigger(conn, event),
                        expected,
                    )
            conn.execute("UPDATE task_events SET relationship = 'consumed'")
            self.assertFalse(
                source_events.should_skip_terminal_trigger(
                    conn,
                    {**event, "actor_kind": "github_system"},
                )
            )

    def test_waiting_task_ignores_consumed_comment_and_resumes_on_new_reply(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.readmodels import workbench

        db_path, task_id, attempt_id = self._seed_active_pr_task(456)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE workstreams SET origin_workstream_id = NULL")
        event = {
            "id": "waiting-request",
            "number": 456,
            "source_type": "pull_request",
            "event_type": "comment",
            "actor_login": "wklken",
            "author_association": "COLLABORATOR",
            "body": "@robert-bot update this branch",
            "intent": "bug_fix",
            "url": "https://github.com/example/backend/pull/456",
            "state": "open",
            "has_open_dd_pr": True,
            "existing_pr_head_branch": "codex/dd-456-task",
            "pr_author_login": "robert-bot",
        }
        self.fixture_path.write_text(json.dumps({"events": [event]}), encoding="utf-8")
        first = run_once.run_once(
            self.config_path,
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET lifecycle = 'waiting_for_user' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'waiting_for_user' WHERE active_task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'completed' WHERE attempt_id = ?",
                (attempt_id,),
            )
            conn.execute(
                "UPDATE task_events SET relationship = 'consumed' WHERE task_id = ?",
                (task_id,),
            )

        replay = run_once.run_once(
            self.config_path,
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["prompt_paths"], [])
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone(),
                ("waiting_for_user", task_id),
            )
            self.assertEqual(
                conn.execute("SELECT relationship FROM task_events WHERE task_id = ?", (task_id,)).fetchone()[0],
                "consumed",
            )
        item = workbench.get_work_item(db_path, "github:example/backend!456")
        self.assertEqual(item["operator_state"]["reason_code"], "waiting_for_user")

        reply = {
            **event,
            "id": "waiting-reply",
            "body": "@robert-bot the dependency is ready; continue",
        }
        self.fixture_path.write_text(json.dumps({"events": [event, reply]}), encoding="utf-8")
        resumed = run_once.run_once(
            self.config_path,
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(len(resumed["prompt_paths"]), 1)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
            child_id, parent_id = conn.execute(
                "SELECT task_id, parent_task_id FROM tasks WHERE task_id != ?",
                (task_id,),
            ).fetchone()
            self.assertEqual(parent_id, task_id)
            self.assertEqual(
                conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone(),
                ("active", child_id),
            )
            fingerprints = conn.execute(
                "SELECT ge.event_fingerprint FROM task_events te JOIN github_events ge ON ge.event_id = te.event_id WHERE te.task_id = ?",
                (child_id,),
            ).fetchall()
            self.assertEqual(fingerprints, [("comment:waiting-reply",)])

from contextlib import closing
import json
import sqlite3
from pathlib import Path
from tests.support import PACKAGE_ROOT
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
    _verification_evidence,
)


class PendingContinuationTests(RepositoryCycleFixture):
    def test_unconsumed_pending_event_creates_child_task_after_parent_result(
        self, legacy_untrusted=False, pending_assignment=False
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
            parent_task_id, parent_attempt_id = conn.execute("SELECT task_id, attempt_id FROM attempts").fetchone()
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "MEMBER",
                            "body": "@robert-bot another point",
                        },
                        {
                            "id": "comment-3",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot one more point",
                        },
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
        if pending_assignment:
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("""UPDATE github_events SET payload_json=json_set(payload_json,
                    '$.actor_login','robert-bot','$.event_type','assigned',
                    '$.assigned_to','robert-bot','$.assignment_actor_login','wklken',
                    '$.body','issue body','$.mentions_dd',json('false'))
                    WHERE event_fingerprint IN ('comment:comment-2','comment:comment-3')""")
        if legacy_untrusted:
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("""UPDATE github_events SET actor_login='external-author',
                    authorization_status='accepted_review_participant',
                    payload_json=json_set(payload_json,'$.actor_login','external-author',
                        '$.authorization_status','accepted_review_participant')
                    WHERE event_fingerprint IN ('comment:comment-2','comment:comment-3')""")
        record = result.record_result(
            db_path,
            {
                "task_id": parent_task_id,
                "attempt_id": parent_attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        parent_task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "done",
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

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute("SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at").fetchall()
            active_task_id = conn.execute("SELECT active_task_id FROM workstreams").fetchone()[0]
            if legacy_untrusted:
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0][2], "completed")
                self.assertIsNone(active_task_id)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM github_events WHERE actor_login='external-author'").fetchone()[
                        0
                    ],
                    2,
                )
                return
            child_events = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.relationship, ge.event_fingerprint
                """,
                (tasks[1][0],),
            ).fetchall()
            prompt_path = Path(
                conn.execute(
                    "SELECT path FROM artifacts WHERE task_id = ? AND artifact_type = 'prompt'",
                    (tasks[1][0],),
                ).fetchone()[0]
            )
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0], (parent_task_id, None, "completed"))
        self.assertEqual(tasks[1][1], parent_task_id)
        self.assertEqual(tasks[1][2], "queued")
        self.assertEqual(active_task_id, tasks[1][0])
        self.assertEqual(
            child_events,
            [("context", "comment:comment-3"), ("trigger", "comment:comment-2")],
        )
        child_prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("comment:comment-2", child_prompt)
        self.assertIn("comment:comment-3", child_prompt)

    def test_saved_trusted_assignment_can_spawn_child_without_mention(self):
        self.test_unconsumed_pending_event_creates_child_task_after_parent_result(pending_assignment=True)

    def test_legacy_participant_pending_events_cannot_spawn_child(self):
        self.test_unconsumed_pending_event_creates_child_task_after_parent_result(legacy_untrusted=True)

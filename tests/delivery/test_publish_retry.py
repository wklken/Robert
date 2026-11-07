from unittest import mock
from contextlib import closing
import json
import sqlite3
from robert_agent.delivery import values as publication_values
from tests.support.publication import PublicationFixture


class PublishRetryTests(PublicationFixture):
    def test_publish_ignores_actions_not_accepted_or_already_published(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET audit_status = 'pending' WHERE action_id = 'action-1'"
            )
        calls = []

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=lambda command, **kwargs: calls.append((command, kwargs)),
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 0)
        self.assertEqual(calls, [])

    def test_publish_failure_keeps_action_retryable_and_records_notification(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        class EmptyComments:
            returncode = 0
            stdout = "[]"
            stderr = ""

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "network unavailable"

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return EmptyComments()
            return Failed()

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "publish_failed")
        self.assertEqual(
            result["failures"],
            [
                {
                    "action_id": "action-1",
                    "safe_error": "network unavailable",
                    "command": [
                        "gh",
                        "api",
                        "repos/x/y/issues/1/comments",
                        "-f",
                        f"body={self._dd_comment_body('ready')}",
                    ],
                }
            ],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status, metadata_json
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(action[0], "not_published")
        publish_metadata = json.loads(action[1])["publish"]
        self.assertEqual(publish_metadata["safe_error"], "network unavailable")
        self.assertEqual(
            publish_metadata["command"],
            [],  # Do not persist command bodies in failure notifications.
        )
        self.assertEqual(notification[:2], ("github_publish_failed", "recorded"))
        notification_metadata = json.loads(notification[2])
        self.assertEqual(notification_metadata["action_id"], "action-1")
        self.assertEqual(
            notification_metadata["command"],
            [],  # Do not persist command bodies in failure notifications.
        )

    def test_repeated_publish_failure_updates_existing_notification(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        class EmptyComments:
            returncode = 0
            stdout = "[]"
            stderr = ""

        class Failed:
            returncode = 1
            stdout = ""

            def __init__(self, stderr):
                self.stderr = stderr

        errors = iter(["network unavailable", "gh auth expired"])

        def fake_run(command, **_kwargs):
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return EmptyComments()
            return Failed(next(errors))

        first = publish.publish_ready_actions(
            db_path, dry_run=False, run_command=fake_run
        )
        # Advance to the persisted retry deadline; immediate retry is now forbidden.
        with closing(sqlite3.connect(db_path)) as retry_conn:
            due = json.loads(
                retry_conn.execute(
                    "SELECT metadata_json FROM github_actions WHERE action_id='action-1'"
                ).fetchone()[0]
            )["publish"]["next_retry_at"]
        retry_clock = mock.patch.object(publication_values, "now", return_value=due)
        retry_clock.start()
        self.addCleanup(retry_clock.stop)
        second = publish.publish_ready_actions(
            db_path, dry_run=False, run_command=fake_run
        )

        self.assertEqual(first["status"], "publish_failed")
        self.assertEqual(second["status"], "publish_failed")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            count, metadata_json = conn.execute(
                """
                SELECT COUNT(*), MAX(metadata_json)
                FROM notifications
                WHERE notification_type = 'github_publish_failed'
                """
            ).fetchone()

        metadata = json.loads(metadata_json)
        self.assertEqual(count, 1)
        self.assertEqual(metadata["action_id"], "action-1")
        self.assertEqual(metadata["safe_error"], "gh auth expired")
        self.assertEqual(
            metadata["command"],
            [],  # Do not persist command bodies in failure notifications.
        )

    def test_publish_success_resolves_previous_failure_notification(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        class EmptyComments:
            returncode = 0
            stdout = "[]"
            stderr = ""

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "network unavailable"

        class Created:
            returncode = 0
            stdout = json.dumps(
                {
                    "id": 987,
                    "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                }
            )
            stderr = ""

        create_results = iter([Failed(), Created()])

        def fake_run(command, **_kwargs):
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return EmptyComments()
            return next(create_results)

        failed = publish.publish_ready_actions(
            db_path, dry_run=False, run_command=fake_run
        )
        # Advance to the persisted retry deadline; immediate retry is now forbidden.
        with closing(sqlite3.connect(db_path)) as retry_conn:
            due = json.loads(
                retry_conn.execute(
                    "SELECT metadata_json FROM github_actions WHERE action_id='action-1'"
                ).fetchone()[0]
            )["publish"]["next_retry_at"]
        retry_clock = mock.patch.object(publication_values, "now", return_value=due)
        retry_clock.start()
        self.addCleanup(retry_clock.stop)
        published = publish.publish_ready_actions(
            db_path, dry_run=False, run_command=fake_run
        )

        self.assertEqual(failed["status"], "publish_failed")
        self.assertEqual(published["status"], "published")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            notification = conn.execute(
                """
                SELECT status, metadata_json
                FROM notifications
                WHERE notification_type = 'github_publish_failed'
                """
            ).fetchone()

        metadata = json.loads(notification[1])
        self.assertEqual(notification[0], "resolved")
        self.assertEqual(metadata["action_id"], "action-1")
        self.assertEqual(metadata["safe_error"], "network unavailable")
        self.assertIn("resolved_at", metadata)
        self.assertEqual(metadata["resolved_publish_status"], "published")

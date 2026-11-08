from contextlib import closing
import json
import sqlite3
from tests.support.publication import PublicationFixture


class PublishCommentTests(PublicationFixture):
    def test_publish_ready_comment_action_marks_published_with_external_id(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        calls = []

        class Completed:
            def __init__(self, stdout):
                self.returncode = 0
                self.stdout = stdout
                self.stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:3] == ["gh", "api", "repos/x/y/issues/1/comments?per_page=100"]:
                return Completed("[]")
            return Completed(
                json.dumps(
                    {
                        "id": 987,
                        "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                    }
                )
            )

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(
            calls[0][0],
            [
                "gh",
                "api",
                "repos/x/y/issues/1/comments?per_page=100",
                "--paginate",
                "--slurp",
            ],
        )
        self.assertEqual(calls[1][0][:3], ["gh", "api", "repos/x/y/issues/1/comments"])
        self.assertIn(f"body={self._dd_comment_body('ready')}", calls[1][0])
        self.assertNotIn("shell", calls[1][1])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT publish_status, external_id, target_url
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(
            row,
            (
                "published",
                "987",
                "https://github.com/x/y/issues/1#issuecomment-987",
            ),
        )

    def test_publish_revalidates_comment_target_against_audited_scope(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(?, '$.result_contract', 'compact-v1') WHERE result_id = 'result-1'",
                (
                    json.dumps(
                        {
                            "audit": {
                                "status": "accepted",
                                "action_scope": {
                                    "repo": "x/y",
                                    "sources": [
                                        {
                                            "source_type": "issue",
                                            "number": 1,
                                        }
                                    ],
                                },
                            }
                        },
                        sort_keys=True,
                    ),
                ),
            )
            conn.execute(
                """
                UPDATE github_actions
                SET target_url = 'https://github.com/other/repo/issues/999',
                    metadata_json = ?
                WHERE action_id = 'action-1'
                """,
                (
                    json.dumps(
                        {
                            "body": self._dd_comment_body("ready"),
                            "target_url": "https://github.com/other/repo/issues/999",
                        },
                        sort_keys=True,
                    ),
                ),
            )
        calls = []

        class Completed:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=fake_run,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "publish_failed")
        self.assertEqual(calls, [])
        self.assertIn("action scope", result["failures"][0]["safe_error"])

    def test_publish_ready_comment_reuses_existing_comment_with_same_marker(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        body = self._dd_comment_body("ready")
        calls = []

        class Completed:
            returncode = 0
            stdout = json.dumps(
                [
                    [
                        {
                            "id": 987,
                            "user": {"login": "robert-bot"},
                            "html_url": "https://github.com/x/y/issues/1#issuecomment-987",
                            "body": body,
                        }
                    ]
                ]
            )
            stderr = ""

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        result = publish.publish_ready_actions(db_path, dry_run=False, run_command=fake_run)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["published_count"], 1)
        self.assertEqual(result["deduplicated_count"], 1)
        self.assertEqual(
            calls,
            [
                (
                    [
                        "gh",
                        "api",
                        "repos/x/y/issues/1/comments?per_page=100",
                        "--paginate",
                        "--slurp",
                    ],
                    {"capture_output": True, "text": True, "timeout": 60},
                )
            ],
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, external_id, target_url, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(
            action[:3],
            ("published", "987", "https://github.com/x/y/issues/1#issuecomment-987"),
        )
        self.assertTrue(json.loads(action[3])["publish"]["deduplicated"])

    def test_publish_ready_comment_without_marker_fails_without_posting(self):
        from robert_agent.delivery.application import publish

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE github_actions
                SET metadata_json = '{"body": "ready without marker"}'
                WHERE action_id = 'action-1'
                """
            )
        calls = []

        result = publish.publish_ready_actions(
            db_path,
            dry_run=False,
            run_command=lambda command, **kwargs: calls.append((command, kwargs)),
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action = conn.execute(
                """
                SELECT publish_status, metadata_json
                FROM github_actions
                WHERE action_id = 'action-1'
                """
            ).fetchone()
        self.assertEqual(action[0], "not_published")
        self.assertIn("robert-comment", json.loads(action[1])["publish"]["safe_error"])

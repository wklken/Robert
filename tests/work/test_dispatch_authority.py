from contextlib import closing
import json
import sqlite3

from robert_agent.persistence import database
from robert_agent.work import dispatch_requests
from tests.support.dispatch_requests import DispatchFixture


NOW = "2026-11-08T06:00:00Z"
ISSUE_KEY = "github:example/repo#7"
ISSUE_URL = "https://github.com/example/repo/issues/7"
REQUEST_BODY = "调研启动慢的原因"


class DispatchAuthorityTests(DispatchFixture):
    def _confirmed_event(self):
        request_id = self.draft()["request_id"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE dispatch_requests SET status = 'issue_created', issue_json = ? WHERE request_id = ?",
                (json.dumps({"number": 7, "html_url": ISSUE_URL}), request_id),
            )
        return {
            "dispatch_request_id": request_id,
            "event_fingerprint": f"operator-dispatch:{request_id}",
            "event_type": "operator_dispatch",
            "actor_kind": "operator",
            "source_key": ISSUE_KEY,
            "source_type": "issue",
            "workstream_id": ISSUE_KEY,
            "number": 7,
            "url": ISSUE_URL,
            "repo": "example/repo",
            "repo_full_name": "example/repo",
            "body": REQUEST_BODY,
        }

    def test_reauthorization_binds_trigger_to_confirmed_request_and_issue(self):
        event = self._confirmed_event()
        with closing(database.connect(self.db_path)) as conn:
            authorized = dispatch_requests.reauthorize(conn, self.config, event)
            self.assertTrue(authorized["drives_execution"])
            self.assertTrue(authorized["creates_task"])

            for override in (
                {"dispatch_request_id": "missing-request"},
                {"event_fingerprint": "unrelated-event"},
                {"event_type": "comment"},
                {"actor_kind": "github_user"},
                {"body": "Replace the user request"},
                {"source_key": "elsewhere/repo#7"},
                {"workstream_id": "elsewhere/repo#7"},
                {"number": 8},
                {"url": "https://github.com/example/repo/issues/8"},
            ):
                with self.subTest(override=override):
                    rejected = dispatch_requests.reauthorize(conn, self.config, {**authorized, **override})
                    self.assertFalse(rejected["drives_execution"])
                    self.assertFalse(rejected["creates_task"])

    def test_revoked_scope_blocks_saved_trigger_and_bound_task(self):
        event = self._confirmed_event()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (ISSUE_KEY, self.repo_id, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) "
                "VALUES ('dispatch-task', ?, 'queued', ?, ?)",
                (ISSUE_KEY, NOW, NOW),
            )
            conn.execute(
                "UPDATE dispatch_requests SET status = 'admitted', workstream_id = ?, task_id = 'dispatch-task' "
                "WHERE request_id = ?",
                (ISSUE_KEY, event["dispatch_request_id"]),
            )

        with closing(database.connect(self.db_path)) as conn:
            self.assertTrue(dispatch_requests.task_allowed(conn, self.config, "dispatch-task"))
            for overrides in (
                {"allowed_senders": ["43"]},
                {"repos": []},
                {"dispatch_enabled": False},
                {"conversation": "telegram:43"},
            ):
                with self.subTest(overrides=overrides):
                    revoked = {**self.config, "messaging": {**self.config["messaging"], **overrides}}
                    self.assertFalse(dispatch_requests.reauthorize(conn, revoked, event)["drives_execution"])
                    self.assertFalse(dispatch_requests.task_allowed(conn, revoked, "dispatch-task"))

    def test_revoked_senders_do_not_starve_eligible_pending_request(self):
        request_id = self.draft()["request_id"]
        self.command(request_id)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            # More old revoked requests than one pending page.
            conn.executemany(
                "INSERT INTO dispatch_requests(request_id, repo_id, identity_json, title, body, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'Old request', 'Old request', 'accepted', ?, ?)",
                [
                    (
                        f"revoked-{index}",
                        self.repo_id,
                        json.dumps({**self.identity, "sender_id": "43"}, sort_keys=True),
                        "2000-01-01T00:00:00Z",
                        "2000-01-01T00:00:00Z",
                    )
                    for index in range(60)
                ],
            )
        restricted = {**self.config, "messaging": {**self.config["messaging"], "allowed_senders": ["42"]}}
        with closing(database.connect(self.db_path)) as conn:
            pending = dispatch_requests.pending(conn, restricted, remote=True, repo_ids=[self.repo_id])
        self.assertEqual([row["request_id"] for row in pending], [request_id])

    def test_revoked_notifications_are_filtered_before_inbox_delivery_and_readiness_limits(self):
        from robert_agent.persistence.queries import notifications
        from robert_agent.delivery import messages
        from robert_agent.work.application import messaging
        from robert_agent.execution import notifications as readiness

        request_id = self.draft()["request_id"]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for index in range(110):
                conn.execute(
                    "INSERT INTO notifications VALUES (?, NULL, 'dispatch_created', 'local', 'recorded', ?, ?)",
                    (
                        f"revoked-{index}",
                        NOW,
                        json.dumps(
                            {
                                "dispatch_identity": {**self.identity, "sender_id": "43"},
                                "request_id": request_id,
                                "repo_id": self.repo_id,
                                "repository_scoped": True,
                            }
                        ),
                    ),
                )
            conn.execute(
                "INSERT INTO notifications VALUES ('current', NULL, 'dispatch_created', 'local', 'recorded', ?, ?)",
                (
                    "2026-11-08T05:00:00Z",
                    json.dumps(
                        {
                            "dispatch_identity": self.identity,
                            "request_id": request_id,
                            "repo_id": self.repo_id,
                            "repository_scoped": True,
                        }
                    ),
                ),
            )
        restricted = {**self.config, "messaging": {**self.config["messaging"], "allowed_senders": ["42"]}}
        self.assertEqual(
            [row["notification_id"] for row in messaging.inbox(restricted, self.identity)["messages"]], ["current"]
        )
        with closing(database.connect(self.db_path)) as conn, conn:
            rows = notifications.find_for_deliver_pending(
                conn,
                NOW,
                1,
                [self.repo_id],
                messaging_repo_ids=messages.delivery_repo_ids(conn, restricted),
                messaging_settings=restricted["messaging"],
            )
            self.assertEqual([row[0] for row in rows], ["current"])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM notifications WHERE notification_id='current'")
        with closing(database.connect(self.db_path)) as conn:
            self.assertFalse(readiness.has_pending_local_delivery(conn, [self.repo_id], NOW, restricted))

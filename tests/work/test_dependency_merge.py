"""An explicit operator reply can wait for a same-repository PR merge."""

from contextlib import closing
import json
import sqlite3
import time

from tests.support.work_items import WorkItemCommandFixture
from robert_agent.adapters.github import client
from robert_agent.work.application import commands
from robert_agent.work import scan_batches

URL = "https://github.com/example/repo/pull/42"


class DependencyMergeTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self.item = self._create(start=True)["item"]
        self.question = self._question(self.item["work_item_id"], 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.task_id, self.updated_at = conn.execute("SELECT task_id,updated_at FROM tasks").fetchone()

    def register(self, **changes):
        return commands.execute_command(
            self.db_path,
            self.item["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=self.item["version"],
            expected_event_id=self.question["event_id"],
            idempotency_key="dependency",
            body="Continue with the original request after the dependency merges.",
            dependency={"pr_url": URL, "task_id": self.task_id, "task_updated_at": self.updated_at, **changes},
        )

    def merged(self, merged=True):
        payload = {
            "state": "closed",
            "html_url": URL,
            "pull_request": {"merged_at": "2026-11-21T10:00:00Z" if merged else None},
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            scan_batches.save_reads(
                conn,
                "repo-1",
                {client.command_key(["gh", "api", "repos/example/repo/issues/42"]): json.dumps(payload)},
                time.time(),
            )

    def resume(self):
        from robert_agent.work.application import dependency_merge

        return dependency_merge.resume_pending(self.db_path, "repo-1")

    def test_registered_reply_waits_then_resumes_once_with_original_question(self):
        result = self.register()
        self.assertIsNone(result["task_id"])
        self.assertEqual(self.resume(), [])
        self.merged(False)
        self.assertEqual(self.resume(), [])
        self.merged()
        (resumed,) = self.resume()
        self.assertEqual(resumed["resolved_event_id"], self.question["event_id"])
        self.assertEqual(self.resume(), [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM wakeups WHERE task_id=?", (resumed["task_id"],)).fetchone()[0], 1
            )

    def test_merge_before_registration_and_registration_replay(self):
        self.merged()
        self.register()
        self.resume()
        self.register()
        self.assertEqual(self.resume(), [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_rejects_cross_repo_noncanonical_and_stale_task(self):
        for changes in (
            {"pr_url": "https://github.com/other/repo/pull/42"},
            {"pr_url": URL + "?x=1"},
            {"pr_url": "https://github.com/example/repo/pull/" + "9" * 30},
            {"task_updated_at": "stale"},
            {"task_id": "other"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((commands.WorkItemValidationError, commands.WorkItemConflictError)),
            ):
                self.register(**changes)

    def test_canceled_or_answered_question_never_resumes(self):
        self.register()
        commands.execute_command(
            self.db_path,
            self.item["work_item_id"],
            context=self.context,
            command="reply",
            expected_version=self.item["version"],
            expected_event_id=self.question["event_id"],
            idempotency_key="ordinary",
            body="Use the ordinary reply instead.",
        )
        self.merged()
        self.assertEqual(self.resume(), [])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_stale_generation_and_cancel_revoke_opt_in(self):
        self.register()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET updated_at='changed'")
        self.merged()
        self.assertEqual(self.resume(), [])
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET updated_at=?", (self.updated_at,))
            conn.execute("UPDATE work_items SET canceled_at='canceled'")
        self.assertEqual(self.resume(), [])

    def test_ordinary_reply_and_registration_cannot_widen_scope(self):
        from robert_agent.work.inputs import CommandContext

        with self.assertRaises(commands.WorkItemValidationError):
            commands.execute_command(
                self.db_path,
                self.item["work_item_id"],
                context=CommandContext("worker", "model", frozenset({"repo-1"}), frozenset()),
                command="reply",
                expected_version=self.item["version"],
                expected_event_id=self.question["event_id"],
                idempotency_key="model",
                body="continue",
                dependency={"pr_url": URL, "task_id": self.task_id, "task_updated_at": self.updated_at},
            )
        with self.assertRaises(commands.WorkItemValidationError):
            commands.execute_command(
                self.db_path,
                self.item["work_item_id"],
                context=self.context,
                command="reply",
                expected_version=self.item["version"],
                expected_event_id=self.question["event_id"],
                idempotency_key="routing",
                body="continue",
                dependency={"pr_url": URL, "task_id": self.task_id, "task_updated_at": self.updated_at},
                routing_mode="manual",
                requested_worker="reviewer",
            )

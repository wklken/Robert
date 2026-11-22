from contextlib import closing
import json
import sqlite3
import unittest

from tests.support.board import BoardReadModelFixture
from robert_agent.persistence.stores import github_sources as source_store
from robert_agent.readmodels import board
from robert_agent.work.application import commands, source_lifecycle


class ClosedSourceDeliveryTests(unittest.TestCase):
    now = "2026-11-21T00:00:00+00:00"

    def fixture(self, kind="issue"):
        fixture = BoardReadModelFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item = fixture._create("Closed " + kind, start=True)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            task = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            provider = "github" if kind == "issue" else "tapd"
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,provider,project_key,external_id,state,metadata_json) "
                "VALUES ('ticket','repo-1','ticket',?,?,?,?,?,'closed',?)",
                (
                    kind,
                    16 if provider == "github" else None,
                    provider,
                    None if provider == "github" else "tapd.example/100",
                    None if provider == "github" else "16",
                    json.dumps({"tapd": {"status": "status_3", "observed_at": self.now}}),
                ),
            )
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE workstreams SET primary_source_id='ticket',lifecycle='completed',active_task_id=NULL")
            conn.execute("UPDATE work_items SET origin_type=?,origin_source_id='ticket'", (provider,))
        return fixture, item, task

    def add_pr(self, fixture, item, number=12, state="open"):
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            source = f"pr-{number}"
            stream = f"github:example/repo!{number}"
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,html_url,state) "
                "VALUES (?,'repo-1',?,'pull_request',?,?,?)",
                (source, stream, number, f"https://github.com/example/repo/pull/{number}", state),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,lifecycle,created_at,updated_at) "
                "VALUES (?,'repo-1',?,?,'completed',?,?)",
                (stream, source, item["workstream_id"], self.now, self.now),
            )
        return source, stream

    def reconcile_ticket(self, fixture):
        from types import SimpleNamespace

        remote = {"state": "closed", "state_reason": "completed", "closed_at": self.now}
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            provider = conn.execute("SELECT provider FROM github_sources WHERE source_id='ticket'").fetchone()[0]
            if provider == "github":
                stream, task = conn.execute("SELECT workstream_id,task_id FROM tasks").fetchone()
                source_lifecycle.record_remote_terminal_state(
                    conn,
                    "repo-1",
                    stream,
                    "ticket",
                    "ticket",
                    "issue",
                    16,
                    task,
                    remote,
                    "completed",
                    "remote_source_closed",
                    self.now,
                )
            else:
                source_lifecycle.reconcile_remote_source_states(
                    conn,
                    "repo-1",
                    {"full_name": "example/repo"},
                    lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(remote), stderr=""),
                    self.now,
                    tapd_only=True,
                )
            conn.commit()

    def test_closed_ticket_with_open_pr_waits_for_review_without_forcing_cancel(self):
        for kind in ("issue", "tapd_story", "tapd_bug"):
            with self.subTest(kind=kind):
                fixture, item, _ = self.fixture(kind)
                self.add_pr(fixture, item)
                self.reconcile_ticket(fixture)
                detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
                self.assertIsNone(detail["completed_at"])
                self.assertEqual(detail["column"], "review")
                self.assertEqual(detail["reason_code"], "open_pr")
                self.assertFalse(detail["status_summary"]["needs_action"])
                self.assertEqual(detail["status_summary"]["primary"], "open_pr")
                self.assertEqual(detail["valid_commands"], ["cancel"])
                with self.assertRaises(commands.WorkItemConflictError):
                    commands.execute_command(
                        fixture.db_path,
                        item["work_item_id"],
                        context=fixture.context,
                        command="request_changes",
                        expected_version=detail["version"],
                        idempotency_key="closed-ticket-new-request",
                        body="Change the implementation",
                    )

    def test_unchanged_closed_tapd_ticket_is_not_reconciled_on_every_scan(self):
        fixture, item, task = self.fixture("tapd_story")
        self.add_pr(fixture, item)

        def reconcile(conn, now):
            return source_lifecycle.reconcile_remote_source_states(
                conn, "repo-1", {"full_name": "example/repo"}, None, now, tapd_only=True
            )

        later = "2026-11-21T00:05:00+00:00"
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            self.assertEqual(reconcile(conn, self.now), 1)
            conn.execute(
                "UPDATE github_sources SET metadata_json=json_set(metadata_json,'$.tapd.observed_at',?) "
                "WHERE source_id='ticket'",
                (later,),
            )
            conn.commit()
            self.assertEqual(reconcile(conn, later), 0)
            self.assertEqual(
                conn.execute("SELECT count(*) FROM audit_events WHERE event_type='remote_source_closed'").fetchone()[0],
                1,
            )
            conn.execute("UPDATE tasks SET lifecycle='queued' WHERE task_id=?", (task,))
            conn.execute(
                "UPDATE workstreams SET lifecycle='active',active_task_id=? WHERE workstream_id=?",
                (task, item["workstream_id"]),
            )
            conn.commit()
            self.assertEqual(reconcile(conn, later), 1)
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "canceled"
            )

    def test_completed_tapd_ticket_without_pr_is_selected_for_terminal_sync(self):
        for kind in ("tapd_story", "tapd_bug"):
            with self.subTest(kind=kind):
                fixture, item, _ = self.fixture(kind)
                self.reconcile_ticket(fixture)
                detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
                self.assertEqual(detail["column"], "done")
                self.assertIsNotNone(detail["completed_at"])

    def test_reopened_tapd_ticket_can_close_again_with_the_same_status(self):
        fixture, item, _ = self.fixture("tapd_story")
        self.add_pr(fixture, item)
        self.reconcile_ticket(fixture)

        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            for observed_at, state, status, expected in (
                ("2026-11-21T00:05:00+00:00", "open", "status_2", 0),
                ("2026-11-21T00:10:00+00:00", "closed", "status_3", 1),
                ("2026-11-21T00:15:00+00:00", "closed", "status_3", 0),
            ):
                source_store.upsert_tapd_source(
                    conn,
                    "ticket",
                    "repo-1",
                    "ticket",
                    "tapd_story",
                    "tapd.example/100",
                    "16",
                    "https://tapd.example/100/prong/stories/view/16",
                    "Closed tapd_story",
                    state,
                    observed_at,
                    json.dumps({"tapd": {"status": status, "observed_at": observed_at}}),
                )
                conn.commit()
                self.assertEqual(
                    source_lifecycle.reconcile_remote_source_states(
                        conn, "repo-1", {"full_name": "example/repo"}, None, observed_at, tapd_only=True
                    ),
                    expected,
                )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM audit_events WHERE event_type='remote_source_closed'").fetchone()[0],
                2,
            )

    def test_open_pr_remains_visible_when_a_newer_derived_pr_is_closed(self):
        fixture, item, _ = self.fixture()
        self.add_pr(fixture, item, 12)
        self.add_pr(fixture, item, 13, "closed")
        detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
        self.assertEqual(detail["reason_code"], "open_pr")
        self.assertEqual(detail["pr"]["number"], 12)

    def test_merging_one_of_multiple_prs_does_not_complete_the_item(self):
        fixture, item, _ = self.fixture()
        source, stream = self.add_pr(fixture, item, 12)
        self.add_pr(fixture, item, 13)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            source_lifecycle.record_remote_terminal_state(
                conn,
                "repo-1",
                stream,
                source,
                stream,
                "pull_request",
                12,
                None,
                {"state": "closed", "merged": True, "merged_at": self.now},
                "completed",
                "remote_pr_merged",
                self.now,
            )
        detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
        self.assertIsNone(detail["completed_at"])
        self.assertEqual(detail["pr"]["number"], 13)
        self.assertEqual(detail["column"], "review")

    def test_last_pr_merge_completes_closed_ticket_delivery(self):
        for kind in ("issue", "tapd_story", "tapd_bug"):
            with self.subTest(kind=kind):
                fixture, item, _ = self.fixture(kind)
                source, stream = self.add_pr(fixture, item)
                self.reconcile_ticket(fixture)
                with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
                    source_lifecycle.record_remote_terminal_state(
                        conn,
                        "repo-1",
                        stream,
                        source,
                        stream,
                        "pull_request",
                        12,
                        None,
                        {"state": "closed", "merged": True, "merged_at": self.now},
                        "completed",
                        "remote_pr_merged",
                        self.now,
                    )
                detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
                self.assertEqual(detail["column"], "done")
                self.assertIsNotNone(detail["completed_at"])

    def test_unmerged_pr_closure_is_not_finished_by_later_ticket_sync(self):
        for kind in ("issue", "tapd_story", "tapd_bug"):
            with self.subTest(kind=kind):
                fixture, item, _ = self.fixture(kind)
                source, stream = self.add_pr(fixture, item)
                with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
                    source_lifecycle.record_remote_terminal_state(
                        conn,
                        "repo-1",
                        stream,
                        source,
                        stream,
                        "pull_request",
                        12,
                        None,
                        {"state": "closed", "merged": False, "closed_at": self.now},
                        "canceled",
                        "remote_pr_closed",
                        self.now,
                    )
                self.reconcile_ticket(fixture)
                detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
                self.assertIsNone(detail["completed_at"])
                self.assertEqual(detail["reason_code"], "unmerged_pr_closed")
                self.assertTrue(detail["status_summary"]["needs_action"])
                self.assertEqual(detail["valid_commands"], ["cancel"])

    def test_pr_merge_preserves_unknown_publication_effect(self):
        fixture, item, task = self.fixture()
        source, stream = self.add_pr(fixture, item)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_actions(action_id,task_id,action_type,audit_status,created_at) "
                "VALUES ('pending-publication',?,'comment','accepted',?)",
                (task, self.now),
            )
            conn.execute(
                "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at) "
                "VALUES('unknown-effect','pending-publication','unknown-effect','fingerprint','executing',?,?)",
                (self.now, self.now),
            )
            source_lifecycle.record_remote_terminal_state(
                conn,
                "repo-1",
                stream,
                source,
                stream,
                "pull_request",
                12,
                None,
                {"state": "closed", "merged": True, "merged_at": self.now},
                "completed",
                "remote_pr_merged",
                self.now,
            )
        detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
        self.assertIsNone(detail["completed_at"])
        self.assertNotEqual(detail["column"], "done")
        with closing(sqlite3.connect(fixture.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM publication_intents").fetchone()[0], "executing")

    def test_deferred_merge_is_reconciled_after_related_task_finishes(self):
        from types import SimpleNamespace

        fixture, item, _ = self.fixture()
        source, stream = self.add_pr(fixture, item)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute("UPDATE work_items SET origin_type='web',origin_source_id=NULL")
            conn.execute(
                "UPDATE workstreams SET primary_source_id=NULL WHERE workstream_id=?", (item["workstream_id"],)
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,lifecycle,created_at,updated_at) "
                "VALUES ('child','repo-1',?,'active',?,?)",
                (item["workstream_id"], self.now, self.now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                "VALUES ('child-task','child','queued',?,?)",
                (self.now, self.now),
            )
            remote = {"state": "closed", "merged": True, "merged_at": self.now}
            source_lifecycle.record_remote_terminal_state(
                conn,
                "repo-1",
                stream,
                source,
                stream,
                "pull_request",
                12,
                None,
                remote,
                "completed",
                "remote_pr_merged",
                self.now,
            )
        detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
        self.assertIsNone(detail["completed_at"])
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id='child-task'")
            conn.execute("UPDATE workstreams SET lifecycle='completed' WHERE workstream_id='child'")
            conn.commit()
            source_lifecycle.reconcile_remote_source_states(
                conn,
                "repo-1",
                {"full_name": "example/repo"},
                lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(remote), stderr=""),
                self.now,
            )
        detail = board.get_work_item_detail(fixture.db_path, item["work_item_id"])
        self.assertEqual(detail["column"], "done")

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from robert_agent.persistence import database, transactions
from robert_agent.persistence.schema.database import init_database
from robert_agent.work.application.commands import (
    ensure_github_work_item,
    ensure_tapd_work_item,
    execute_command,
    record_system_event,
)
from robert_agent.work.errors import WorkItemNotFoundError, WorkItemValidationError
from robert_agent.work.inputs import COMMANDS, CommandContext
from tests.support.source_cycle import SourceCycleFixture

NOW = "2026-11-15T00:00:00+08:00"


class TapdWorkItemTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Path(scratch.name) / "db.sqlite"
        init_database(self.db)
        self.sql("""
            INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root,enabled)
              VALUES('a','org/a','bot','main','/a','/at',1),('b','org/b','bot','main','/b','/bt',1),
                    ('off','org/off','bot','main','/o','/ot',0);
            INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id,title)
              VALUES('ticket','tapd:ticket','tapd','tapd.example','001','bug','002','Ticket title');
            INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id,repo_id,number)
              VALUES('gh','github:org/a#1','github','github.com','org/a','issue','1','a',1);
            INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,created_at,updated_at)
              VALUES('gh-stream','a','gh','now','now');
        """)

    def sql(self, statements):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(statements)

    @contextmanager
    def writer(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            yield conn

    def item(self, conn, repo, source="ticket"):
        return ensure_tapd_work_item(conn, repo_id=repo, source_id=source, now=NOW)

    def test_same_real_source_has_distinct_per_repository_items(self):
        with self.writer() as conn:
            a = self.item(conn, "a")
            b = self.item(conn, "b")
            self.assertEqual(self.item(conn, "a"), a)
        self.assertNotEqual(a["work_item_id"], b["work_item_id"])
        self.assertNotEqual(a["workstream_id"], b["workstream_id"])
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute(
                "SELECT w.repo_id,i.origin_type,w.primary_source_id,x.relationship FROM work_items i "
                "JOIN workstreams w USING(workstream_id) JOIN workstream_sources x USING(workstream_id) "
                "WHERE i.origin_source_id='ticket' ORDER BY w.repo_id"
            ).fetchall()
            self.assertEqual(rows, [("a", "tapd", "ticket", "primary"), ("b", "tapd", "ticket", "primary")])
            self.assertEqual(conn.execute("SELECT count(*) FROM work_item_events").fetchone(), (2,))
            self.assertEqual(conn.execute("SELECT count(*) FROM sources WHERE provider='tapd'").fetchone(), (1,))

    def test_existing_item_revalidated_against_real_provider_and_enabled_repository(self):
        with self.writer() as conn:
            self.item(conn, "a")
            for repo, source in (("a", "gh"), ("a", "missing"), ("unknown", "ticket"), ("off", "ticket")):
                with self.subTest(repo=repo, source=source), self.assertRaises(WorkItemValidationError):
                    self.item(conn, repo, source)
        self.sql("UPDATE repos SET enabled=0 WHERE repo_id='a';")
        with self.writer() as conn, self.assertRaises(WorkItemValidationError):
            self.item(conn, "a")

    def test_repo_only_command_cannot_cancel_native_item(self):
        with self.writer() as conn:
            item = self.item(conn, "a")
        context = CommandContext("operator", "repo-operator", frozenset({"a"}), frozenset())
        identifier = item["work_item_id"]
        with self.assertRaisesRegex(WorkItemNotFoundError, "^work item not found$"):
            execute_command(
                self.db,
                identifier,
                context=context,
                command="cancel",
                expected_version=item["version"],
                idempotency_key="native-cancel",
            )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT version,canceled_at FROM work_items WHERE work_item_id=?", (identifier,)
                ).fetchall(),
                [(item["version"], None)],
            )
            self.assertEqual(conn.execute("SELECT event_type FROM work_item_events").fetchall(), [("tapd_intake",)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups").fetchone(), (0,))

    def test_repo_only_command_cannot_replay_native_item_result(self):
        with self.writer() as conn:
            item = self.item(conn, "a")
            record_system_event(
                conn,
                item["work_item_id"],
                event_type="canceled",
                idempotency_key="native-old-command",
                metadata={"command_result": {"ok": True, "item": item, "task_id": None}},
                now=NOW,
            )
        context = CommandContext("operator", "repo-operator", frozenset({"a"}), frozenset())
        for command in sorted(COMMANDS):
            with self.subTest(command=command), self.assertRaisesRegex(WorkItemNotFoundError, "^work item not found$"):
                execute_command(
                    self.db,
                    item["work_item_id"],
                    context=context,
                    command=command,
                    expected_version=item["version"] + 1,
                    idempotency_key="native-old-command",
                )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone(), (2,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone(), (0,))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wakeups").fetchone(), (0,))

    def test_inconsistent_native_item_cannot_fall_back_to_legacy_commands(self):
        with self.writer() as conn:
            item = self.item(conn, "a")
        context = CommandContext("operator", "repo-operator", frozenset({"a"}), frozenset())

        def refused(case):
            with self.subTest(case=case), self.assertRaises(WorkItemNotFoundError):
                execute_command(
                    self.db,
                    item["work_item_id"],
                    context=context,
                    command="cancel",
                    expected_version=item["version"],
                    idempotency_key="malformed-native-cancel",
                )

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE work_items SET origin_type='github' WHERE work_item_id=?", (item["work_item_id"],))
        refused("native origin source with inconsistent stored origin type")

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE work_items SET origin_source_id=NULL WHERE work_item_id=?", (item["work_item_id"],))
        refused("native primary stream with missing item origin source")

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE workstreams SET primary_source_id=NULL WHERE workstream_id=?", (item["workstream_id"],)
            )
        refused("native primary relationship with missing direct primary source column")

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE work_items SET origin_type='tapd',workstream_id=NULL WHERE work_item_id=?",
                (item["work_item_id"],),
            )
        refused("declared native item with missing source and workstream")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT event_type FROM work_item_events").fetchall(), [("tapd_intake",)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone(), (0,))

    def test_historical_github_command_replay_is_withheld_after_native_stream_adoption(self):
        self.sql("""
            INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id,repo_id,number)
              VALUES('pr','github:org/a!7','github','github.com','org/a','pull_request','7','a',7);
            INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,created_at,updated_at)
              VALUES('pr-stream','a','pr','now','now');
        """)
        with self.writer() as conn:
            native = self.item(conn, "a")
            ordinary = ensure_github_work_item(
                conn,
                repo_id="a",
                source_id="pr",
                workstream_id="pr-stream",
                actor_identity="owner",
                route_confidence="high",
                now=NOW,
            )
        context = CommandContext("operator", "repo-operator", frozenset({"a"}), frozenset())

        def cancel():
            return execute_command(
                self.db,
                ordinary["work_item_id"],
                context=context,
                command="cancel",
                expected_version=ordinary["version"],
                idempotency_key="legacy-pr-cancel",
            )

        first = cancel()
        self.assertIsNotNone(first["item"]["canceled_at"])
        self.assertEqual(cancel(), first)
        # The historical WorkItem keeps its original provider when its stream
        # later acquires a native origin. Only the command boundary changes.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE workstreams SET origin_workstream_id=? WHERE workstream_id='pr-stream'",
                (native["workstream_id"],),
            )
        with self.assertRaises(WorkItemNotFoundError):
            cancel()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT origin_type,origin_source_id,canceled_at FROM work_items WHERE work_item_id=?",
                    (ordinary["work_item_id"],),
                ).fetchone(),
                ("github", "pr", first["item"]["canceled_at"]),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM work_item_events WHERE work_item_id=? AND actor_kind='operator'",
                    (ordinary["work_item_id"],),
                ).fetchone(),
                (1,),
            )

    def test_related_tapd_reference_preserves_ordinary_github_commands(self):
        self.sql("""
            INSERT INTO workstream_sources(workstream_id,source_id,relationship,created_at)
              VALUES('gh-stream','ticket','related','now');
        """)
        with self.writer() as conn:
            item = ensure_github_work_item(
                conn,
                repo_id="a",
                source_id="gh",
                workstream_id="gh-stream",
                actor_identity="owner",
                route_confidence="high",
                now=NOW,
            )
        context = CommandContext("operator", "repo-operator", frozenset({"a"}), frozenset())
        result = execute_command(
            self.db,
            item["work_item_id"],
            context=context,
            command="cancel",
            expected_version=item["version"],
            idempotency_key="ordinary-related-cancel",
        )
        self.assertIsNotNone(result["item"]["canceled_at"])

    def test_index_upgrade_is_quiescence_guarded_and_preserves_legacy_identity(self):
        with self.writer() as conn:
            github = ensure_github_work_item(
                conn,
                repo_id="a",
                source_id="gh",
                workstream_id="gh-stream",
                actor_identity="owner",
                route_confidence="high",
                now=NOW,
            )
            initial = self.item(conn, "a")
        self.sql("""
            DROP INDEX idx_work_items_origin_source;
            DROP INDEX idx_work_items_tapd_origin_repo;
            CREATE UNIQUE INDEX idx_work_items_origin_source
              ON work_items(origin_source_id) WHERE origin_source_id IS NOT NULL;
            INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at)
              VALUES('running','gh-stream','running','now','now');
            INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('running','running',1,'running');
        """)
        with self.assertRaisesRegex(RuntimeError, "Worker attempt"):
            init_database(self.db)
        self.sql("UPDATE attempts SET status='completed';")
        init_database(self.db)
        init_database(self.db)
        with self.writer() as conn:
            self.assertEqual(self.item(conn, "a"), initial)
            self.assertNotEqual(self.item(conn, "b")["work_item_id"], initial["work_item_id"])
            preserved = ensure_github_work_item(
                conn,
                repo_id="a",
                source_id="gh",
                workstream_id="gh-stream",
                actor_identity="owner",
                route_confidence="high",
                now=NOW,
            )
            self.assertEqual(preserved, github)
        with closing(sqlite3.connect(self.db)) as conn:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "UNIQUE constraint failed: work_items.origin_source_id"
            ):
                conn.execute(
                    "INSERT INTO work_items(work_item_id,repo_id,title,origin_type,origin_source_id,"
                    "created_by,created_at,updated_at,creation_idempotency_key) "
                    "VALUES('duplicate','b','Title','web','gh','user','now','now','separate-key')"
                )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class NativeChildCommandScopeTests(SourceCycleFixture):
    def test_ordinary_cancel_cannot_touch_displaced_native_child_membership(self):
        self.config(development=True)
        self.tick("admitted", ticket=self.ticket)
        prepared = self.tick("prepared", ticket=self.ticket)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                INSERT INTO sources(source_id,source_key,provider,host,project_key,source_type,external_id,repo_id,number)
                  VALUES('gh','github:org/alpha#7','github','github.com','org/alpha','issue','7','repo:org/alpha',7);
                INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,created_at,updated_at)
                  VALUES('gh-root','repo:org/alpha','gh','now','now');
                INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,created_at,updated_at)
                  VALUES('gh-child','repo:org/alpha','gh-root','now','now');
            """)
            conn.execute("UPDATE tasks SET workstream_id='gh-child' WHERE task_id=?", (prepared.identity,))
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            item = ensure_github_work_item(
                conn,
                repo_id="repo:org/alpha",
                source_id="gh",
                workstream_id="gh-root",
                actor_identity="owner",
                route_confidence="high",
                now=NOW,
            )
        context = CommandContext("operator", "repo-operator", frozenset({"repo:org/alpha"}), frozenset())
        with self.assertRaises(WorkItemNotFoundError):
            execute_command(
                self.db,
                item["work_item_id"],
                context=context,
                command="cancel",
                expected_version=item["version"],
                idempotency_key="legacy-parent-cancel",
            )
        self.assertEqual(
            self.read("SELECT lifecycle,workstream_id FROM tasks WHERE task_id=?", (prepared.identity,)),
            [("queued", "gh-child")],
        )
        self.assertEqual(
            self.read("SELECT round_id FROM source_round_tasks WHERE task_id=?", (prepared.identity,)),
            [(prepared.round_id,)],
        )
        self.assertEqual(
            self.read("SELECT version,canceled_at FROM work_items WHERE work_item_id=?", (item["work_item_id"],)),
            [(item["version"], None)],
        )

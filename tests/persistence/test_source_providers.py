"""Source rows carry a provider identity, and legacy tables are rebuilt without losing rows."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence.schema import database as storage
from robert_agent.persistence.schema import source_providers
from tests.support.database import connect

_LEGACY_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/persistence/pre_orm_5e0e952.sql"
_REBUILT_TABLES = ("github_sources", "work_items")


def _insert_repo(conn):
    conn.execute(
        "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) "
        "VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees')"
    )


def _insert_source(conn, source_id, **values):
    row = {"repo_id": "repo-1", "source_key": source_id, "source_type": "issue", "number": 1, **values}
    columns = ", ".join(["source_id", *row])
    marks = ", ".join("?" * (len(row) + 1))
    conn.execute(f"INSERT INTO github_sources({columns}) VALUES ({marks})", (source_id, *row.values()))


def _table_sql(conn, table):
    return conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]


class SourceProviderSchemaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "robert.sqlite3"
        storage.init_database(self.db)

    def test_existing_github_writes_default_to_github_and_tapd_rows_use_external_identity(self):
        with connect(self.db) as conn:
            _insert_repo(conn)
            _insert_source(conn, "github-1")
            _insert_source(
                conn,
                "tapd-1",
                provider="tapd",
                source_type="tapd_story",
                number=None,
                project_key="tapd.example/100",
                external_id="1100",
            )
            rows = conn.execute(
                "SELECT source_id, provider, source_type, number, project_key, external_id "
                "FROM github_sources ORDER BY source_id"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("github-1", "github", "issue", 1, None, None),
                ("tapd-1", "tapd", "tapd_story", None, "tapd.example/100", "1100"),
            ],
        )

    def test_source_identity_must_match_its_provider(self):
        tapd = {"provider": "tapd", "source_type": "tapd_bug", "number": None, "project_key": "p", "external_id": "1"}
        invalid = {
            "github without number": {"number": None},
            "github with external identity": {"project_key": "p", "external_id": "1"},
            "github with tapd type": {"source_type": "tapd_story"},
            "tapd with number": {**tapd, "number": 7},
            "tapd without external id": {**tapd, "external_id": None},
            "tapd with github type": {**tapd, "source_type": "issue"},
            "unknown provider": {"provider": "gitlab"},
        }
        with connect(self.db) as conn:
            _insert_repo(conn)
        for name, values in invalid.items():
            with self.subTest(name), connect(self.db) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    _insert_source(conn, "invalid", **values)

    def test_work_items_accept_tapd_origin(self):
        with connect(self.db) as conn:
            _insert_repo(conn)
            conn.execute(
                "INSERT INTO work_items(work_item_id, repo_id, title, origin_type, creation_idempotency_key, "
                "created_by, created_at, updated_at) VALUES ('wi-1', 'repo-1', 'Ticket', 'tapd', 'key-1', "
                "'operator', '2026-11-15T00:00:00+00:00', '2026-11-15T00:00:00+00:00')"
            )
            self.assertEqual(conn.execute("SELECT origin_type FROM work_items").fetchone()[0], "tapd")

    def test_workstream_public_refs_are_unique_when_present(self):
        def insert(conn, workstream_id, public_ref):
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, public_ref, created_at, updated_at) "
                "VALUES (?, 'repo-1', ?, '2026-11-15T00:00:00+00:00', '2026-11-15T00:00:00+00:00')",
                (workstream_id, public_ref),
            )

        with connect(self.db) as conn:
            _insert_repo(conn)
            insert(conn, "github-1", None)
            insert(conn, "github-2", None)
            insert(conn, "tapd-1", "rbt-1")
        with self.assertRaises(sqlite3.IntegrityError), connect(self.db) as conn:
            insert(conn, "tapd-2", "rbt-1")


class LegacySourceTableRebuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "robert.sqlite3"
        with connect(self.db) as conn:
            conn.executescript(_LEGACY_FIXTURE.read_text(encoding="utf-8"))

    def _snapshot(self, path=None):
        with connect(path or self.db) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            rows = {table: sorted(tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')) for table in tables}
            indexes = {
                table: sorted(
                    tuple(row)
                    for row in conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (table,)
                    )
                )
                for table in tables
            }
        return rows, indexes

    def test_rebuild_keeps_every_row_reference_and_index(self):
        rows, indexes = self._snapshot()

        storage.init_database(self.db)

        rebuilt_rows, rebuilt_indexes = self._snapshot()
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            sources = conn.execute(
                "SELECT source_id, repo_id, source_key, source_type, number, html_url, title, state, author_login, "
                "source_updated_at, metadata_json FROM github_sources ORDER BY source_id"
            ).fetchall()
            providers = conn.execute("SELECT provider, project_key, external_id FROM github_sources").fetchall()
            self.assertIn("'tapd'", _table_sql(conn, "work_items"))
            references = conn.execute(
                "SELECT e.event_id, w.workstream_id, s.source_id, i.work_item_id "
                "FROM github_events e JOIN github_sources g ON g.source_id = e.source_id "
                "JOIN workstreams w ON w.primary_source_id = g.source_id "
                "JOIN workstream_sources s ON s.source_id = g.source_id "
                "JOIN work_items i ON i.origin_source_id = g.source_id "
                "JOIN work_item_events ie ON ie.work_item_id = i.work_item_id"
            ).fetchall()
        self.assertEqual([tuple(row) for row in sources], rows["github_sources"])
        self.assertEqual([tuple(row) for row in providers], [("github", None, None)])
        self.assertEqual(len(references), 1)
        self.assertEqual(rebuilt_rows["work_items"], rows["work_items"])
        self.assertEqual([row[:-1] for row in rebuilt_rows["workstreams"]], rows["workstreams"])
        self.assertEqual({row[-1] for row in rebuilt_rows["workstreams"]}, {None})
        for table in rows.keys() - {*_REBUILT_TABLES, "workstreams"}:
            with self.subTest(table=table):
                self.assertEqual(rebuilt_rows[table], rows[table])
        self.assertLessEqual(set(indexes["work_items"]), set(rebuilt_indexes["work_items"]))

        with connect(self.db) as conn:
            rebuilt = [_table_sql(conn, table) for table in _REBUILT_TABLES]
        storage.init_database(self.db)
        with connect(self.db) as conn:
            self.assertEqual([_table_sql(conn, table) for table in _REBUILT_TABLES], rebuilt)

    def test_rebuild_first_writes_a_backup_of_the_legacy_database(self):
        rows, indexes = self._snapshot()

        storage.init_database(self.db)

        backup = source_providers.backup_path(self.db)
        self.assertEqual(self._snapshot(backup), (rows, indexes))
        with connect(backup) as conn:
            self.assertNotIn("provider", _table_sql(conn, "github_sources"))

    def test_rebuild_is_refused_while_a_process_may_still_write(self):
        cases = {
            "active lease": (
                "INSERT INTO leases VALUES ('lease-live', 'daemon', 'database', 'daemon-1', 'active', "
                "'2026-11-15T00:00:00+00:00', '2999-01-01T00:00:00+00:00', '2026-11-15T00:00:00+00:00', NULL, '{}')",
                "lease",
            ),
            "running attempt": ("UPDATE attempts SET status = 'running'", "attempt"),
            "stale attempt": ("UPDATE attempts SET status = 'stale'", "attempt"),
            "unproven model child": (
                "INSERT INTO review_sessions(round_id, repo, pr_number, workstream_id, trigger_fingerprint, "
                "request_json, created_at, updated_at) VALUES ('round-1', 'example/backend', 7, 'ws', 'fp', '{}', "
                "'2026-11-15T00:00:00+00:00', '2026-11-15T00:00:00+00:00');"
                "INSERT INTO review_runs(run_id, round_id, stage, worker_id, attempt_no, owner_attempt_id, status, "
                "output_dir, prompt_sha256, slot_reserved, created_at, updated_at) VALUES ('run-1', 'round-1', "
                "'review', 'worker', 1, 'attempt-b213140d8d0c', 'completed', '/out', 'sha', 1, "
                "'2026-11-15T00:00:00+00:00', '2026-11-15T00:00:00+00:00')",
                "review run",
            ),
        }
        for name, (statement, reason) in cases.items():
            with self.subTest(name):
                self.setUp()
                with connect(self.db) as conn:
                    conn.executescript(statement)
                before = self._snapshot()

                with self.assertRaisesRegex(source_providers.RebuildRefused, reason):
                    storage.init_database(self.db)

                self.assertEqual(self._snapshot(), before)
                self.assertFalse(source_providers.backup_path(self.db).exists())

    def test_foreign_key_violation_rolls_back_the_rebuild(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) "
                "VALUES ('orphan', 'repo:missing', 'github:missing#1', 'issue', 1)"
            )
        before = self._snapshot()

        with self.assertRaisesRegex(source_providers.RebuildRefused, "foreign key"):
            storage.init_database(self.db)

        self.assertEqual(self._snapshot(), before)

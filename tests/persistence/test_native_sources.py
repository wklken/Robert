"""Persistence contracts for provider-scoped sources before repo inference."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.persistence import database
from robert_agent.persistence.queries import sources
from robert_agent.persistence.schema.database import init_database
from robert_agent.persistence.stores import sources as source_store


class NativeSourcesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "sources.sqlite3"
        init_database(self.db)
        self.identity = {
            "host": "tapd.example",
            "project_key": "12345678",
            "source_type": "story",
            "external_id": "001000123400000000000000000001",
        }

    def record(self, conn, *, identity=None, name="first", **changes):
        identity = self.identity if identity is None else identity
        return source_store.upsert_tapd_source(
            conn,
            source_id=f"source:{name}",
            source_key=f"tapd:{name}",
            html_url=None,
            state="open",
            **{"title": "Original", **changes},
            **identity,
        )

    def test_scoped_source_identity_and_repo_less_event(self):
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            original = self.record(conn)
            update = self.record(conn, title="Updated", metadata={"version": 2})
            self.assertEqual(original["source_id"], update["source_id"])
            self.assertEqual(update["title"], "Updated")
            self.assertEqual(update["metadata_json"], '{"version": 2}')
            for field, value in (
                ("host", "another.example"),
                ("project_key", "another-project"),
                ("source_type", "bug"),
            ):
                identity = {**self.identity, field: value}
                alternate = self.record(conn, identity=identity, name=field)
                self.assertEqual(alternate["external_id"], self.identity["external_id"])
                self.assertIsNone(alternate["repo_id"])
                self.assertIsNone(alternate["number"])
            with self.assertRaisesRegex(ValueError, "existing ID or key"):
                self.record(conn, name="wrong-id")
            with self.assertRaises(sqlite3.IntegrityError):
                self.record(conn, identity={**self.identity, "external_id": "other"})
            with self.assertRaisesRegex(ValueError, "strings"):
                self.record(conn, identity={**self.identity, "external_id": 12345678})

        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO source_events(event_id, source_id, event_fingerprint, event_type, actor_kind,"
                "event_at, payload_json) VALUES('event',?,'tapd:event','comment','tapd_user','2026-11-14',?)",
                (original["source_id"], json.dumps({"external_id": self.identity["external_id"], "body": "原文"})),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT repo_id, json_extract(payload_json, '$.external_id') FROM source_events"
                ).fetchone(),
                (None, self.identity["external_id"]),
            )
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

        with closing(database.connect(self.db)) as conn:
            row = sources.find_by_identity(conn, provider="tapd", **self.identity)
            self.assertEqual(row, update)
            self.assertIsNone(sources.find_by_identity(conn, provider="github", **self.identity))

    def test_provider_and_kind_are_enforced_in_database(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
                " VALUES('r','owner/repo','bot','main','/repo','/trees')"
            )
            for provider, kind, number, external_id, repo_id in (
                ("github", "bug", 1, "1", "r"),
                ("github", "issue", 1, "01", "r"),
                ("github", "issue", 1, "1", None),
                ("tapd", "pull_request", None, "1", None),
                ("tapd", "story", 1, "1", None),
                ("unknown", "story", None, "1", None),
            ):
                with self.subTest(provider=provider, kind=kind, number=number, repo_id=repo_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO sources(source_id,source_key,provider,source_type,host,project_key,"
                            "external_id,number,repo_id) VALUES('id','key',?,?,'host','project',?,?,?)",
                            (provider, kind, external_id, number, repo_id),
                        )
            conn.execute(
                "INSERT INTO work_items(work_item_id,repo_id,title,origin_type,creation_idempotency_key,created_by,"
                "created_at,updated_at) VALUES('item','r','Story','tapd','dedupe','operator','now','now')"
            )

from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from robert_agent.persistence.schema.database import init_database
from robert_agent.models import Base
from robert_agent.models.discovery import GitHubReadCache
from robert_agent.models.work import Repository


# FTS5 owns these tables. They are optional and are never used as ORM entities.
_FTS_TABLES = {
    "project_memory_fts",
    "project_memory_fts_config",
    "project_memory_fts_content",
    "project_memory_fts_data",
    "project_memory_fts_docsize",
    "project_memory_fts_idx",
}
_SQL_TOKEN = re.compile(r"'(?:[^']|'')*'|\w+|[^\s]")


def _check_expressions(ddl):
    tokens = _SQL_TOKEN.findall(ddl)
    checks = Counter()
    for start, token in enumerate(tokens):
        if token.upper() != "CHECK" or tokens[start + 1 : start + 2] != ["("]:
            continue
        depth = 1
        for end in range(start + 2, len(tokens)):
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            if depth == 0:
                checks[tuple(tokens[start + 2 : end])] += 1
                break
    return checks


def _table_definitions(conn):
    return dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"))


def _columns(conn, table):
    return conn.execute("SELECT * FROM pragma_table_xinfo(?) ORDER BY cid", (table,)).fetchall()


def _foreign_keys(conn, table):
    groups = defaultdict(list)
    for key_id, seq, *parts in conn.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,)):
        groups[key_id].append((seq, *parts))
    return Counter(tuple(sorted(group)) for group in groups.values())


def _indexes(conn, table):
    indexes = Counter()
    for _, name, unique, origin, partial in conn.execute("SELECT * FROM pragma_index_list(?)", (table,)):
        columns = conn.execute(
            'SELECT cid, name, "desc", coll FROM pragma_index_xinfo(?) WHERE "key" = 1 ORDER BY seqno',
            (name,),
        ).fetchall()
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)).fetchone()[0]
        # Explicit index DDL carries both expression keys and partial predicates.
        definition = tuple(_SQL_TOKEN.findall(ddl)) if ddl else ()
        indexes[(unique, origin, partial, tuple(columns), definition)] += 1
    return indexes


class SchemaMappingTests(unittest.TestCase):
    def test_mapped_ddl_matches_initialized_database(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "initialized.sqlite3"
            mapped = Path(directory) / "mapped.sqlite3"
            init_database(existing)
            engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(mapped))
            try:
                # This database is disposable. Production initialization still uses SQL migrations.
                Base.metadata.create_all(engine)
            finally:
                engine.dispose()

            with closing(sqlite3.connect(existing)) as original, closing(sqlite3.connect(mapped)) as model:
                originals = _table_definitions(original)
                models = _table_definitions(model)
                self.assertEqual(set(models), originals.keys() - _FTS_TABLES)
                for table, ddl in models.items():
                    with self.subTest(table=table):
                        self.assertEqual(_columns(model, table), _columns(original, table))
                        self.assertEqual(_foreign_keys(model, table), _foreign_keys(original, table))
                        self.assertEqual(_check_expressions(ddl), _check_expressions(originals[table]))
                        self.assertEqual(_indexes(model, table), _indexes(original, table))

    def test_mapped_reads_and_writes_keep_existing_storage_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "existing.sqlite3"
            init_database(database)
            payload = '{"b": 2, "a": "保留原文本"}'
            expiration = 1_700_000_000.125
            with closing(sqlite3.connect(database)) as original, original:
                original.execute(
                    "INSERT INTO github_read_cache (scope, cache_key, version, payload_json, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("account", "issue", "etag", payload, expiration),
                )

            engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(database))
            try:
                with Session(engine) as session, session.begin():
                    cache = session.scalars(select(GitHubReadCache)).one()
                    self.assertEqual(cache.payload_json, payload)
                    self.assertEqual(cache.expires_at, expiration)
                    session.add(
                        Repository(
                            repo_id="repo",
                            full_name="example/repo",
                            github_account="account",
                            default_base_branch="main",
                            repo_root="repo",
                            worktree_root="worktrees",
                            metadata_json=payload,
                        )
                    )
            finally:
                engine.dispose()

            with closing(sqlite3.connect(database)) as original:
                self.assertEqual(
                    original.execute("SELECT enabled, metadata_json, typeof(metadata_json) FROM repos").fetchone(),
                    (1, payload, "text"),
                )

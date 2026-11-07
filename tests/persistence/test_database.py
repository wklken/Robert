from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from urllib.parse import quote

from sqlalchemy import Integer, Text, bindparam, column, event, insert, select, table
from sqlalchemy.engine import Engine

from robert_agent.persistence import database, errors, statements
from robert_agent.persistence.records import Record


_RECORDS = table("records", column("id", Integer), column("value", Text))
_INSERT = insert(_RECORDS).values(id=bindparam("p0"), value=bindparam("p1"))
_READ = select(_RECORDS).order_by(_RECORDS.c.id)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "existing records.sqlite3"
        with closing(sqlite3.connect(self.path)) as legacy:
            legacy.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    def test_checkpoint_and_rollback_keep_the_same_physical_transaction(self):
        with (
            closing(database.connect(self.path)) as writer,
            closing(database.connect(self.path)) as reader,
        ):
            with self.assertRaisesRegex(RuntimeError, "suffix failed"), writer:
                self.assertEqual(statements.write(writer, _INSERT, (1, "checkpoint")), 1)
                self.assertTrue(writer.in_transaction)
                self.assertEqual(statements.read(writer, _READ), [(1, "checkpoint")])
                self.assertEqual(statements.read(reader, _READ), [])
                writer.commit()
                self.assertFalse(writer.in_transaction)
                self.assertEqual(statements.read(reader, _READ), [(1, "checkpoint")])
                statements.write(writer, _INSERT, (2, "rolled back"))
                statements.write(writer, _INSERT, (3, "also rolled back"))
                raise RuntimeError("suffix failed")
            self.assertFalse(writer.in_transaction)
            self.assertEqual(statements.read(reader, _READ), [(1, "checkpoint")])
            with writer:
                statements.write(writer, _INSERT, {"p0": 4, "p1": "committed"})
            self.assertEqual(statements.read(reader, _READ), [(1, "checkpoint"), (4, "committed")])

    def test_repeated_connections_share_compilation_and_keep_transactions_separate(self):
        compiled_reads = []

        def capture(_conn, _cursor, _sql, _params, context, _many):
            if context.compiled is not None and context.compiled.statement is _READ:
                compiled_reads.append(context.compiled)

        event.listen(Engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, Engine, "before_cursor_execute", capture)
        with closing(database.connect(self.path)) as reader:
            with closing(database.connect(os.fspath(self.path))) as writer:
                self.assertIsNot(reader._driver, writer._driver)
                statements.write(writer, _INSERT, (1, "uncommitted"))
                self.assertEqual(statements.read(writer, _READ), [(1, "uncommitted")])
                self.assertEqual(statements.read(reader, _READ), [])
            self.assertEqual(statements.read(reader, _READ), [])
        self.assertEqual(len(compiled_reads), 3)
        self.assertIs(compiled_reads[0], compiled_reads[1])
        self.assertIs(compiled_reads[1], compiled_reads[2])

    def test_relative_filename_and_uri_follow_the_current_directory(self):
        nested = self.path.parent / "nested"
        nested.mkdir()
        with closing(sqlite3.connect(nested / self.path.name)) as raw, raw:
            raw.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            raw.execute("INSERT INTO records VALUES (1, 'nested')")

        relative_uri = f"file:{quote(self.path.name)}?mode=rw"
        original = os.getcwd()
        try:
            for folder, expected in ((self.path.parent, []), (nested, [(1, "nested")])):
                os.chdir(folder)
                with closing(database.connect(Path(self.path.name))) as conn:
                    self.assertEqual(statements.read(conn, _READ), expected)
                with closing(database.connect(relative_uri, uri=True)) as conn:
                    self.assertEqual(statements.read(conn, _READ), expected)
        finally:
            os.chdir(original)

    def test_connections_to_the_same_file_keep_their_requested_options(self):
        for option, pragma, first, second, expected in (
            ("timeout", "busy_timeout", 0.125, 0.25, (125, 250)),
            ("busy_timeout_ms", "busy_timeout", 30, 40, (30, 40)),
            ("foreign_keys", "foreign_keys", True, False, (1, 0)),
        ):
            with self.subTest(option=option):
                for value, actual in zip((first, second), expected):
                    with closing(database.connect(self.path, **{option: value})) as conn:
                        with closing(conn._driver.execute(f"PRAGMA {pragma}")) as result:
                            self.assertEqual(result.fetchone()[0], actual)

    def test_immediate_writer_lock_and_legacy_reads(self):
        with closing(database.connect(self.path, timeout=0.01)) as first:
            with closing(database.connect(self.path, timeout=0.01)) as second:
                statements.read(first, _READ)
                self.assertFalse(first.in_transaction)
                # The dialect can start an immediate transaction even if a SELECT
                # has triggered SQLAlchemy's logical autobegin on the connection.
                first._connection.exec_driver_sql("BEGIN IMMEDIATE").close()
                self.assertTrue(first.in_transaction)
                with self.assertRaises(errors.OperationalError):
                    statements.write(second, _INSERT, (1, "blocked"))
                second.rollback()
                first.rollback()
                with second:
                    self.assertEqual(statements.write(second, _INSERT, (1, "unblocked")), 1)

    def test_readonly_and_missing_existing_file_are_enforced(self):
        with closing(database.connect(self.path.as_uri() + "?mode=rw", uri=True)) as writable:
            self.assertEqual(statements.read(writable, _READ), [])
        with closing(database.connect(self.path.as_uri() + "?mode=ro", uri=True)) as readonly:
            self.assertEqual(statements.read(readonly, _READ), [])
            with self.assertRaises(errors.OperationalError):
                statements.write(readonly, _INSERT, (1, "refused"))
        missing = self.path.with_name("missing.sqlite3")
        with self.assertRaises(errors.OperationalError):
            database.connect(missing.as_uri() + "?mode=rw", uri=True)
        self.assertFalse(missing.exists())

    def test_batch_counts_original_driver_errors_and_close_rolls_back(self):
        with closing(database.connect(self.path)) as writer:
            self.assertEqual(statements.write_many(writer, _INSERT, [(1, "one"), (2, "two")]), 2)
            with self.assertRaises(sqlite3.IntegrityError) as failure:
                statements.write(writer, _INSERT, (1, "duplicate"))
            self.assertIsInstance(failure.exception, errors.IntegrityError)
        with self.assertRaises(sqlite3.ProgrammingError):
            statements.read(writer, _READ)
        with closing(sqlite3.connect(self.path)) as legacy:
            self.assertEqual(legacy.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)

    def test_borrowed_connection_keeps_owner_transaction_and_row_factory(self):
        with closing(sqlite3.connect(self.path)) as legacy:
            legacy.row_factory = lambda cursor, row: {"legacy": row}
            factory = legacy.row_factory
            legacy.execute("INSERT INTO records VALUES (?, ?)", (1, "first"))
            self.assertEqual(statements.write_many(legacy, _INSERT, [(2, "second"), (3, "third")]), 2)
            selection = _READ.where(_RECORDS.c.id.in_(bindparam("ids", expanding=True)))
            selected = statements.read(legacy, selection, {"ids": [2, 3]})
            self.assertEqual(selected, [(2, "second"), (3, "third")])
            row = selected[0]
            self.assertIsInstance(row, Record)
            self.assertEqual(row[0], 2)
            self.assertEqual(row[:1], (2,))
            self.assertEqual(row["value"], "second")
            self.assertEqual(dict(row), {"id": 2, "value": "second"})
            self.assertIs(legacy.row_factory, factory)
            self.assertTrue(legacy.in_transaction)
            legacy.rollback()
            self.assertEqual(statements.read(legacy, _READ), [])

    def test_explicit_transaction_mode_does_not_make_implicit_writes_pending(self):
        with closing(database.connect(self.path)) as reader:
            self.assertEqual(statements.read(reader, _READ), [])
        with closing(database.connect(self.path, isolation_level=None)) as writer:
            statements.write(writer, _INSERT, (1, "autocommitted"))
            self.assertFalse(writer.in_transaction)
            writer._connection.exec_driver_sql("BEGIN IMMEDIATE").close()
            statements.write(writer, _INSERT, (2, "pending"))
            self.assertTrue(writer.in_transaction)
            writer.rollback()
        with closing(sqlite3.connect(self.path)) as legacy:
            self.assertEqual(
                legacy.execute("SELECT * FROM records").fetchall(),
                [(1, "autocommitted")],
            )

    def test_failed_commit_rolls_back_the_physical_transaction(self):
        with closing(sqlite3.connect(self.path)) as legacy:
            legacy.execute(
                "CREATE TABLE dependents (record_id INTEGER REFERENCES records(id) DEFERRABLE INITIALLY DEFERRED)"
            )
        dependents = table("dependents", column("record_id", Integer))
        with closing(database.connect(self.path, foreign_keys=True, busy_timeout_ms=125)) as writer:
            with closing(writer._driver.execute("PRAGMA busy_timeout")) as result:
                self.assertEqual(result.fetchone()[0], 125)
            self.assertFalse(writer.in_transaction)
            with self.assertRaises(errors.IntegrityError), writer:
                statements.write(writer, insert(dependents).values(record_id=42))
            self.assertFalse(writer.in_transaction)
            self.assertEqual(statements.read(writer, select(dependents)), [])
            with writer:
                statements.write(writer, _INSERT, (42, "reused"))
                statements.write(writer, insert(dependents).values(record_id=42))

"""Direct SQLite access for independent fixture setup and persisted-state assertions."""

from contextlib import closing, contextmanager
import sqlite3


@contextmanager
def connect(path):
    with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn

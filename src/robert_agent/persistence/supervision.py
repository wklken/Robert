"""adapters sqlite supervision responsibilities."""

from robert_agent.persistence import database as db_database
from robert_agent.persistence.dialects import sqlite as db_sqlite
from pathlib import Path


def connect(db_path, *, readonly=False):
    path = Path(db_path).resolve()
    # mode=rw refuses to create a missing database, just as mode=ro does.
    conn = db_database.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=0.1)
    db_sqlite.configure(conn, foreign_keys=True)
    return conn

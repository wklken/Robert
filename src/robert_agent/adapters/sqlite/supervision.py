"""adapters sqlite supervision responsibilities."""
from pathlib import Path
import sqlite3



def connect(db_path, *, readonly=False):
    path = Path(db_path).resolve()
    # mode=rw refuses to create a missing database, just as mode=ro does.
    conn = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=0.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

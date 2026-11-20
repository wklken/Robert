"""Durable receiver for the configured notification command."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys


def main():
    delivery = json.load(sys.stdin)
    path = Path("/state/notification-sink.sqlite3")
    with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS effects (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO calls VALUES (?)", (delivery["delivery_id"],))
        count = conn.execute("SELECT count(*) FROM calls WHERE id=?", (delivery["delivery_id"],)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO effects VALUES (?, ?)", (delivery["delivery_id"], json.dumps(delivery)))
    return int(count == 1)


if __name__ == "__main__":
    raise SystemExit(main())

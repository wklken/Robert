"""Savepoint cleanup after durable external I/O checkpoints."""

import sqlite3


def finish_isolation(conn, name, *, rollback=False):
    """Undo only uncommitted work; external-I/O checkpoints stay durable."""
    try:
        if rollback:
            conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
    except sqlite3.OperationalError as exc:
        if str(exc) != f"no such savepoint: {name}":
            raise
        # A helper committed its checkpoint and released the savepoint. This
        # transaction contains only the current source/repository's suffix.
        if rollback:
            conn.rollback()


def begin_immediate(conn):
    conn.execute("BEGIN IMMEDIATE")


def savepoint(conn, name):
    conn.execute(f"SAVEPOINT {name}")

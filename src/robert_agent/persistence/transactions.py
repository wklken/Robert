"""Explicit writer locks and savepoint cleanup around durable checkpoints."""

import re

from robert_agent.persistence import errors
from robert_agent.persistence.dialects.sqlite import execute_control


def _name(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("invalid savepoint name")
    return name


def begin(conn, *, immediate=False):
    execute_control(conn, "BEGIN IMMEDIATE" if immediate else "BEGIN")


def begin_immediate(conn):
    begin(conn, immediate=True)


def savepoint(conn, name):
    execute_control(conn, f"SAVEPOINT {_name(name)}")


def release(conn, name):
    execute_control(conn, f"RELEASE {_name(name)}")


def rollback_to(conn, name):
    execute_control(conn, f"ROLLBACK TO {_name(name)}")


def finish_isolation(conn, name, *, rollback=False):
    """Undo only uncommitted work; external-I/O checkpoints stay durable."""
    try:
        if rollback:
            rollback_to(conn, name)
        release(conn, name)
    except errors.OperationalError as exc:
        if str(exc) != f"no such savepoint: {name}":
            raise
        if rollback:
            conn.rollback()


def begin_exclusive(conn):
    execute_control(conn, "BEGIN EXCLUSIVE")

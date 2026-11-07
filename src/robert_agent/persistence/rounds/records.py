"""Shared review/vote record access with fixed table and field contracts."""

from sqlalchemy import select, update

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def round_table(name):
    if name not in {"review_sessions", "review_runs", "vote_sessions", "vote_runs"}:
        raise ValueError("unknown model round table")
    return tables[name]


def session(conn, table_name, *, round_id=None, task_id=None):
    if not round_id and task_id is None:
        return None
    table = round_table(table_name)
    condition = table.c.round_id == round_id if round_id else table.c.task_id == task_id
    return records.first(statements.read(conn, select(table).where(condition).limit(1)))


def update_session(conn, table_name, round_id, fields):
    table = round_table(table_name)
    allowed = {
        "task_id",
        "status",
        "manifest_json",
        "report_path",
        "report_sha256",
        "error",
        "updated_at",
    }
    if table_name == "vote_sessions":
        allowed.add("request_json")
    if not set(fields) <= allowed:
        raise ValueError("invalid round session fields")
    return statements.write(conn, update(table).where(table.c.round_id == round_id).values(fields))


def update_run(conn, table_name, run_id, fields):
    table = round_table(table_name)
    # Caller-specific contracts constrain this further before this shared write.
    if not set(fields) <= set(table.c.keys()) - {"run_id", "round_id"}:
        raise ValueError("invalid round run fields")
    return statements.write(conn, update(table).where(table.c.run_id == run_id).values(fields))

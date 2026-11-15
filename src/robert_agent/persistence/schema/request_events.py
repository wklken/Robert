"""Lift old task-local consumption into separately scoped request-event rows."""

import hashlib


VERSION = 8
NAME = "robert-request-event-consumption-v1"
CHECKSUM = hashlib.sha256(NAME.encode("utf-8")).hexdigest()


def has_legacy_bindings(conn):
    # The old table permitted pending/consumed even on empty databases.
    row = conn.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name='task_events'").fetchone()
    if row is None:
        return False
    ddl = row[0].lower()
    if "'pending'" in ddl and "'consumed'" in ddl:
        return True
    if "relationship in ('trigger', 'context')" in ddl:
        return False
    # Hand-built early schemas may omit the old CHECK.
    return (
        conn.execute("SELECT 1 FROM task_events WHERE relationship IN ('pending','consumed') LIMIT 1").fetchone()
        is not None
    )


def migrate(conn, schema, execute_script):
    """Copy facts once, retaining IDs and any original result evidence."""
    if conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Request event migration requires a dedicated connection with foreign keys disabled")
    if conn.execute("SELECT 1 FROM request_events LIMIT 1").fetchone():
        raise RuntimeError("Request event migration found an existing ledger alongside legacy task state")

    prefix = "CREATE TABLE IF NOT EXISTS task_events ("
    start = schema.index(prefix) + len(prefix)
    end = schema.index("\n);", start)
    conn.execute("CREATE TABLE new_task_events (" + schema[start:end] + "\n)")
    conn.execute(
        """
        INSERT INTO new_task_events(task_id,event_id,relationship,created_at)
        SELECT task_id,event_id,CASE WHEN relationship IN ('pending','consumed') THEN 'context'
            ELSE relationship END,created_at
        FROM task_events
        """
    )
    conn.execute(
        """
        WITH old_consumption AS (
          SELECT te.task_id, te.event_id, te.relationship, te.created_at,
            CASE WHEN te.relationship = 'consumed' THEN (
              SELECT wr.result_id FROM worker_results wr
              WHERE wr.task_id = te.task_id AND EXISTS (
                SELECT 1
                FROM json_each(CASE WHEN json_valid(wr.consumed_event_fingerprints_json)
                  THEN wr.consumed_event_fingerprints_json ELSE '[]' END) evidence
                WHERE evidence.value = se.event_fingerprint
              )
              ORDER BY wr.created_at DESC, wr.result_id DESC LIMIT 1
            ) END AS result_id
          FROM task_events te LEFT JOIN source_events se ON se.event_id = te.event_id
          WHERE te.relationship IN ('pending','consumed')
        )
        INSERT INTO request_events(task_id,event_id,state,created_at,consumed_at,result_id)
        SELECT old.task_id,old.event_id,old.relationship,old.created_at,wr.created_at,old.result_id
        FROM old_consumption old LEFT JOIN worker_results wr ON wr.result_id = old.result_id
        """
    )
    for replacement, condition in (
        ("new_task_events", ""),
        ("request_events", "WHERE relationship IN ('pending','consumed')"),
    ):
        expected = conn.execute(f"SELECT count(*) FROM task_events {condition}").fetchone()[0]
        actual = conn.execute(f"SELECT count(*) FROM {replacement}").fetchone()[0]
        if actual != expected:
            raise RuntimeError(f"Request event migration changed {replacement} row count from {expected} to {actual}")
    objects = conn.execute(
        "SELECT sql FROM sqlite_schema WHERE tbl_name='task_events' AND type IN ('trigger','index') AND sql IS NOT NULL"
    ).fetchall()
    conn.execute("DROP TABLE task_events")
    conn.execute("ALTER TABLE new_task_events RENAME TO task_events")
    for (sql,) in objects:
        execute_script(conn, sql)

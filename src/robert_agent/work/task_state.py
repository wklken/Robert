"""Task state writes owned by the work context."""


def mark_repository_busy(conn, repo_id, now, expires_at):
    rows = conn.execute(
        """SELECT t.task_id FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
           WHERE w.repo_id=? AND t.lifecycle IN ('queued','running')""",
        (repo_id,),
    ).fetchall()
    for (task_id,) in rows:
        conn.execute(
            """UPDATE tasks
               SET metadata_json=json_set(metadata_json,'$.wait_state.reason_code','repository_busy',
                   '$.wait_state.since',COALESCE(json_extract(metadata_json,'$.wait_state.since'),?),
                   '$.wait_state.lease_expires_at',?)
               WHERE task_id=?""",
            (now, expires_at, task_id),
        )


def clear_repository_busy(conn, repo_id):
    conn.execute(
        """UPDATE tasks SET metadata_json=json_remove(metadata_json,'$.wait_state')
           WHERE task_id IN (SELECT t.task_id FROM tasks t
                             JOIN workstreams w ON w.workstream_id=t.workstream_id WHERE w.repo_id=?)
             AND json_extract(metadata_json,'$.wait_state.reason_code')='repository_busy'""",
        (repo_id,),
    )


def complete_waiting_parent(conn, task_id, now):
    conn.execute(
        "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
        (now, task_id),
    )


def repository_task_ids(conn, repo_id):
    return [
        task_id
        for (task_id,) in conn.execute(
            """SELECT task_id FROM tasks
           WHERE workstream_id IN (SELECT workstream_id FROM workstreams WHERE repo_id=?)
             AND lifecycle IN ('queued','running')""",
            (repo_id,),
        ).fetchall()
    ]


def event_is_attached(conn, event_id):
    return (
        conn.execute(
            "SELECT 1 FROM task_events WHERE event_id=? LIMIT 1", (event_id,)
        ).fetchone()
        is not None
    )

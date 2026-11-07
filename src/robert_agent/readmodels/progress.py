"""Read aggregate controller progress without claiming or changing work."""


def _count(conn, table, where="1 = 1", params=()):
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}",
        params,
    ).fetchone()[0]


def snapshot_progress(conn):
    return {
        "pending_wakeups": _count(conn, "wakeups", "status = 'pending'"),
        "consumed_wakeups": _count(conn, "wakeups", "status = 'consumed'"),
        "expired_wakeups": _count(conn, "wakeups", "status = 'expired'"),
        "prepared_attempts": _count(conn, "attempts", "status = 'prepared'"),
        "running_attempts": _count(conn, "attempts", "status = 'running'"),
        "stale_attempts": _count(conn, "attempts", "status = 'stale'"),
        "completed_attempts": _count(conn, "attempts", "status = 'completed'"),
        "failed_attempts": _count(conn, "attempts", "status = 'failed'"),
        "pending_actions": _count(conn, "github_actions", "audit_status = 'pending'"),
        "accepted_unpublished_actions": _count(
            conn,
            "github_actions",
            "audit_status = 'accepted' AND publish_status = 'not_published'",
        ),
        "published_actions": _count(conn, "github_actions", "publish_status = 'published'"),
        "running_tasks": _count(conn, "tasks", "lifecycle IN ('queued', 'running')"),
        "completed_tasks": _count(conn, "tasks", "lifecycle = 'completed'"),
        "failed_tasks": _count(conn, "tasks", "lifecycle = 'failed'"),
        "active_workstreams": _count(conn, "workstreams", "lifecycle = 'active'"),
        "waiting_workstreams": _count(conn, "workstreams", "lifecycle = 'waiting_for_user'"),
        "completed_workstreams": _count(conn, "workstreams", "lifecycle = 'completed'"),
    }


def diff_progress(before, after):
    changed = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    return {"changed": bool(changed), "changed_fields": changed}

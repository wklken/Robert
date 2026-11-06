"""Read task and source facts without importing the runtime driver."""
import json


def load_active_workstreams(conn, repo_id):
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT workstream_id
            FROM workstreams
            WHERE repo_id = ?
              AND lifecycle IN ('active', 'waiting_for_user')
              AND active_task_id IS NOT NULL
            """,
            (repo_id,),
        )
    }


def load_known_workstreams(conn, repo_id):
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT workstream_id
            FROM workstreams
            WHERE repo_id = ?
              AND (lifecycle != 'canceled' OR json_extract(metadata_json,'$.remote_reconciliation') IS NOT NULL)
            """,
            (repo_id,),
        )
    }


def task_related_events(conn, task_id, include_consumed=False):
    relationships = ["trigger", "context", "pending"]
    if include_consumed:
        relationships.append("consumed")
    placeholders = ", ".join("?" for _ in relationships)
    rows = conn.execute(
        f"""
        SELECT te.relationship, ge.event_fingerprint, ge.payload_json
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND te.relationship IN ({placeholders})
        ORDER BY CASE te.relationship
            WHEN 'trigger' THEN 0
            WHEN 'consumed' THEN 1
            WHEN 'context' THEN 2
            ELSE 3
          END,
          ge.event_at,
          ge.event_id
        """,
        (task_id, *relationships),
    ).fetchall()
    events = []
    consumed_events = []
    for relationship, event_fingerprint, payload in rows:
        event = json.loads(payload)
        event.setdefault("event_fingerprint", event_fingerprint)
        if relationship == "consumed":
            consumed_events.append(event)
        else:
            events.append(event)
    # Enrich recovery content while retaining the original trigger authority.
    metadata = conn.execute('SELECT metadata_json FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    refreshed = json.loads(metadata[0] or '{}').get('refreshed_context', []) if metadata else []
    by_fingerprint = {item['event_fingerprint']: item for item in refreshed}
    for event in events + consumed_events:
        snapshot = by_fingerprint.get(event.get('event_fingerprint'), {})
        event.update({key: snapshot[key] for key in ('body', 'title', 'source_updated_at',
            'body_observed', 'body_updated_at', 'source_body', 'operator_responses') if key in snapshot})
    return events + consumed_events


def failed_parent_context_events(conn, parent_task_id):
    if not parent_task_id:
        return []
    row = conn.execute(
        "SELECT lifecycle FROM tasks WHERE task_id = ?",
        (parent_task_id,),
    ).fetchone()
    if not row or row[0] != "failed":
        return []
    rows = conn.execute(
        """
        SELECT te.relationship, ge.event_fingerprint, ge.payload_json
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND te.relationship IN ('trigger', 'context', 'pending')
        ORDER BY CASE te.relationship
            WHEN 'trigger' THEN 0
            WHEN 'context' THEN 1
            ELSE 2
          END,
          ge.event_at,
          ge.event_id
        """,
        (parent_task_id,),
    ).fetchall()
    events = []
    for relationship, event_fingerprint, payload in rows:
        event = json.loads(payload)
        event.setdefault("event_fingerprint", event_fingerprint)
        if relationship == "trigger" or event.get("mentions_dd"):
            events.append(event)
    return events


def latest_task_id(conn, workstream_id):
    row = conn.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE workstream_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    return row[0] if row else None


def active_task_id(conn, workstream_id):
    row = conn.execute(
        "SELECT active_task_id FROM workstreams WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        """
        SELECT task_id
        FROM tasks
        WHERE workstream_id = ?
          AND lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    return row[0] if row else None


def remote_source_rows(conn, repo_id):
    return conn.execute(
        """
        SELECT
          w.workstream_id,
          w.active_task_id,
          gs.source_id,
          gs.source_key,
          gs.source_type,
          gs.number
        FROM workstreams w
        JOIN github_sources gs ON gs.source_id = w.primary_source_id
        WHERE w.repo_id = ?
          AND (
            (
              w.active_task_id IS NOT NULL
              AND w.lifecycle IN ('active', 'waiting_for_user')
            )
            OR (
              gs.source_type = 'pull_request'
              AND gs.state = 'open'
            )
            OR (
              gs.source_type = 'issue'
              AND EXISTS (
                SELECT 1 FROM work_items wi
                WHERE wi.origin_source_id = gs.source_id
                  AND wi.completed_at IS NULL AND wi.canceled_at IS NULL
              )
            )
          )
        ORDER BY w.updated_at, w.workstream_id
        """,
        (repo_id,),
    ).fetchall()

"""Persist source events and read their associated task state."""
from robert_agent.adapters.github import discovery as discover
from robert_agent.work.application import commands as work_items
from robert_agent.work import control_queries
from robert_agent.foundation import values
from robert_agent.work import policies as work_policies
from robert_agent.work import queries as work_queries
import json


def event_has_terminal_trigger(conn, event_fingerprint):
    row = conn.execute(
        """
        SELECT 1
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        JOIN tasks t ON t.task_id = te.task_id
        WHERE ge.event_fingerprint = ?
          AND (
            te.relationship = 'consumed'
            OR (te.relationship = 'trigger'
                AND t.lifecycle IN ('completed', 'failed', 'canceled', 'ignored'))
          )
        LIMIT 1
        """,
        (event_fingerprint,),
    ).fetchone()
    return bool(row)


def should_skip_terminal_trigger(conn, event):
    if not event_has_terminal_trigger(
        conn,
        event["event_fingerprint"],
    ):
        return False
    return event.get("actor_kind") != "github_system"


def active_task_state(conn, workstream_id):
    row = conn.execute(
        """
        SELECT t.task_id, t.lifecycle, t.route_id, t.expected_output,
               a.attempt_id, a.status
        FROM tasks t
        LEFT JOIN attempts a ON a.task_id = t.task_id
        WHERE t.workstream_id = ?
          AND t.lifecycle IN ('detected', 'authorized', 'classified', 'queued', 'running')
        ORDER BY t.created_at DESC, a.attempt_no DESC
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "task_id": row[0],
        "task_lifecycle": row[1],
        "route_id": row[2],
        "expected_output": row[3],
        "attempt_id": row[4],
        "attempt_status": row[5],
    }


def waiting_task_state(conn, workstream_id):
    row = conn.execute(
        """
        SELECT t.task_id
        FROM workstreams w
        JOIN tasks t ON t.task_id = w.active_task_id
        WHERE w.workstream_id = ?
          AND w.lifecycle = 'waiting_for_user'
          AND t.lifecycle = 'waiting_for_user'
        LIMIT 1
        """,
        (workstream_id,),
    ).fetchone()
    if not row:
        return None
    return {"task_id": row[0]}


def task_review_input_policy(conn, task_id):
    row = conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return values.json_object(row[0] if row else None).get("review_input_policy", "required")


def insert_source_and_event(conn, repo_id, event, run_now):
    source_id = values.github_source_id(event["source_key"])
    conn.execute(
        """
        INSERT INTO github_sources(
          source_id, repo_id, source_key, source_type, number, html_url,
          title, state, author_login, source_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
          html_url = COALESCE(excluded.html_url, github_sources.html_url),
          title = excluded.title,
          state = CASE WHEN ? IS NULL THEN github_sources.state ELSE excluded.state END,
          author_login = excluded.author_login,
          source_updated_at = COALESCE(excluded.source_updated_at, github_sources.source_updated_at)
        """,
        (
            source_id,
            repo_id,
            event["source_key"],
            event["source_type"],
            event["number"],
            event.get("url"),
            event.get("title", ""),
            event.get("state") or "open",
            event.get("source_author_login", event.get("actor_login")),
            event.get("source_updated_at"),
            event.get("state"),
        ),
    )
    event_id = values.github_event_id(event["event_fingerprint"])
    previous = conn.execute(
        "SELECT payload_json FROM github_events WHERE event_fingerprint = ?",
        (event["event_fingerprint"],),
    ).fetchone()
    if previous:
        # Routing and prompt creation must use the same body as durable context.
        event.update(discover.merge_event_body(event, json.loads(previous[0])))
    conn.execute(
        """
        INSERT INTO github_events(
          event_id, repo_id, source_id, event_fingerprint, event_type, actor_kind,
          actor_login,
          author_association, authorization_status, event_at, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_fingerprint) DO UPDATE SET
          authorization_status = CASE
            WHEN github_events.authorization_status IN (
              'authorized_trigger',
              'authorized_system_trigger'
            )
              THEN github_events.authorization_status
            ELSE excluded.authorization_status
          END,
          actor_kind = CASE
            WHEN github_events.authorization_status IN (
              'authorized_trigger',
              'authorized_system_trigger'
            )
              THEN github_events.actor_kind
            ELSE excluded.actor_kind
          END,
          author_association = CASE
            WHEN github_events.authorization_status IN (
              'authorized_trigger',
              'authorized_system_trigger'
            )
              AND excluded.authorization_status NOT IN (
                'authorized_trigger',
                'authorized_system_trigger'
              )
              THEN github_events.author_association
            ELSE excluded.author_association
          END,
          payload_json = CASE
            WHEN github_events.authorization_status IN (
              'authorized_trigger',
              'authorized_system_trigger'
            )
              AND excluded.authorization_status NOT IN (
                'authorized_trigger',
                'authorized_system_trigger'
              )
              THEN github_events.payload_json
            ELSE excluded.payload_json
          END
        """,
        (
            event_id,
            repo_id,
            source_id,
            event["event_fingerprint"],
            event["event_type"],
            event.get("actor_kind", "github_user"),
            event.get("actor_login"),
            event.get("author_association"),
            event.get("authorization_status", "pending"),
            event.get("event_at", run_now),
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        ),
    )
    return source_id, event_id


def event_existing_pr_head_sha(event):
    head_sha = event.get("existing_pr_head_sha") or event.get("head_sha")
    return head_sha if isinstance(head_sha, str) else ""


def operator_canceled_item(conn, item):
    if not item or not item['canceled_at']:
        return False
    return bool(conn.execute("SELECT 1 FROM work_item_events WHERE work_item_id=? AND event_type='canceled' AND actor_kind!='system' LIMIT 1", (item['work_item_id'],)).fetchone())


def github_followup_allows_new_subject(conn, repo, event, parent_task_id):
    """Admit a fresh trusted request, preserving the prior execution's state."""
    if (not parent_task_id or not work_policies.is_driving_context_event(event)
            or not work_policies.is_trusted_actor(event, repo)
            or event.get('actor_kind') == 'github_system'
            or event.get('actor_login') == repo['github_account']):
        return False
    if conn.execute('SELECT 1 FROM task_events WHERE event_id=? LIMIT 1',
                    (values.github_event_id(event['event_fingerprint']),)).fetchone():
        return False
    parent = conn.execute('''SELECT t.lifecycle,w.lifecycle,w.active_task_id,t.metadata_json
        FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
        WHERE t.task_id=? AND w.workstream_id=? AND w.repo_id=?''',
        (parent_task_id, event['workstream_id'], values.repository_id(repo['full_name']))).fetchone()
    remote_canceled = bool(parent and parent[0] == 'canceled' and values.json_object(parent[3]).get('remote_reconciliation'))
    if (not parent or (parent[0] not in {'completed', 'failed'} and not remote_canceled)
            or (parent[1] not in {'completed', 'failed'} and not remote_canceled) or parent[2]
            or work_queries.latest_task_id(conn, event['workstream_id']) != parent_task_id):
        return False
    item = work_items.resolve_work_item_for_workstream(conn, event['workstream_id'])
    if not item or (item['canceled_at'] and (not remote_canceled or operator_canceled_item(conn, item))):
        return False
    facts = control_queries.lifecycle_facts(conn, item)
    if (facts['running'] or facts['actions'] or facts['cleanup_pending']
            or facts['workflow_attention'] or facts['source_closed']):
        return False
    placeholders = ','.join('?' for _ in facts['streams'])
    return not conn.execute(f'''SELECT 1 FROM tasks WHERE workstream_id IN ({placeholders})
        AND lifecycle NOT IN ('completed','failed','canceled','ignored') LIMIT 1''',
        facts['streams']).fetchone()

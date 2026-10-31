"""Durable Kanban requests awaiting GitHub context from the remote scanner."""
from datetime import datetime
import json

from robert_agent import execution_store, github_scan, work_item_control


def pending_tasks(conn, repo_ids=(), *, task_id=None):
    clause = ' AND w.repo_id IN (' + ','.join('?' for _ in repo_ids) + ')' if repo_ids else ''
    if task_id:
        clause += ' AND t.task_id=?'
    cursor = conn.execute(f"""SELECT t.task_id,t.parent_task_id,t.workstream_id,t.metadata_json,
        wu.wakeup_id,wu.created_at AS requested_at,wi.work_item_id,wi.origin_source_id,
        w.repo_id,r.full_name,gs.number,gs.source_type
        FROM tasks t JOIN workstreams w ON w.active_task_id=t.task_id AND w.workstream_id=t.workstream_id
        JOIN repos r ON r.repo_id=w.repo_id
        JOIN work_items wi ON wi.workstream_id=w.workstream_id
        JOIN github_sources gs ON gs.source_id=wi.origin_source_id
        JOIN wakeups wu ON wu.task_id=t.task_id AND wu.work_item_id=wi.work_item_id
        WHERE wi.origin_type='github' AND wi.canceled_at IS NULL AND wi.completed_at IS NULL
          AND t.lifecycle='detected' AND t.task_kind='human_request'
          AND wu.reason='manual_operator_request' AND wu.status='pending'
          AND json_extract(t.metadata_json,'$.trigger_work_item_event_id') IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.task_id=t.task_id)
          AND {execution_store.runnable_sql()} {clause}
        ORDER BY t.created_at,t.task_id""", [*repo_ids, *([task_id] if task_id else [])])
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def source_read(conn, task):
    """Require a scanner observation after the accepted operator request."""
    key = github_scan.command_key(['gh', 'api', f"repos/{task['full_name']}/issues/{task['number']}"])
    row = conn.execute('''SELECT response_json FROM github_scan_reads
        WHERE repo_id=? AND command_key=? AND observed_at>=?''',
        (task['repo_id'], key, datetime.fromisoformat(task['requested_at']).timestamp()),
    ).fetchone()
    # No row means wait for the scanner; a row containing null is a failed read.
    try:
        response = json.loads(row[0]) if row and row[0] else None
    except (ValueError, TypeError):
        response = None
    return row is not None, response


def waiting_wakeup_ids(conn, repo_ids=()):
    """A waiting or blocked request must not drive repeated empty local cycles."""
    return [task['wakeup_id'] for task in pending_tasks(conn, repo_ids)
            if blocked(conn, task) or not source_read(conn, task)[0]]


def blocked(conn, task):
    facts = work_item_control.lifecycle_facts(conn, task)
    return bool(facts['running'] or facts['actions'] or facts['workflow_attention'] or facts['cleanup_pending'])

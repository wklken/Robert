"""Durable Kanban requests awaiting GitHub context from the remote scanner."""

from robert_agent.persistence.queries import operator_requests

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_scan_reads as db_read_github_scan_reads
from robert_agent.work import control_queries
from robert_agent.adapters.github import client as github_client
from datetime import datetime
import json


def pending_tasks(conn, repo_ids=(), *, task_id=None):
    return [dict(row) for row in operator_requests.pending_tasks(conn, repo_ids, task_id)]


def source_read(conn, task):
    """Require a scanner observation after the accepted operator request."""
    key = github_client.command_key(["gh", "api", f"repos/{task['full_name']}/issues/{task['number']}"])
    row = db_records.first(
        db_read_github_scan_reads.find_for_source_read(
            conn, task["repo_id"], key, datetime.fromisoformat(task["requested_at"]).timestamp()
        )
    )
    # No row means wait for the scanner; a row containing null is a failed read.
    try:
        response = json.loads(row[0]) if row and row[0] else None
    except (ValueError, TypeError):
        response = None
    return row is not None, response


def waiting_wakeup_ids(conn, repo_ids=()):
    """A waiting or blocked request must not drive repeated empty local cycles."""
    return [
        task["wakeup_id"]
        for task in pending_tasks(conn, repo_ids)
        if blocked(conn, task) or not source_read(conn, task)[0]
    ]


def blocked(conn, task):
    facts = control_queries.lifecycle_facts(conn, task)
    return bool(facts["running"] or facts["actions"] or facts["workflow_attention"] or facts["cleanup_pending"])

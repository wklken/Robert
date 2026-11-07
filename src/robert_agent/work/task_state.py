"""Task state writes owned by the work context."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.stores import tasks as db_write_tasks


def mark_repository_busy(conn, repo_id, now, expires_at):
    rows = db_read_tasks.mark_repository_busy(conn, repo_id)
    for (task_id,) in rows:
        db_write_tasks.mark_repository_busy(conn, now, expires_at, task_id)


def clear_repository_busy(conn, repo_id):
    db_write_tasks.update_for_clear_repository_busy(conn, repo_id)


def complete_waiting_parent(conn, task_id, now):
    db_write_tasks.update_for_finish_parent_for_child(conn, now, task_id)


def repository_task_ids(conn, repo_id):
    return [task_id for (task_id,) in db_read_tasks.find_for_repository_task_ids(conn, repo_id)]


def event_is_attached(conn, event_id):
    return db_records.first(db_read_task_events.find_for_prepare_queued_reviews(conn, event_id)) is not None

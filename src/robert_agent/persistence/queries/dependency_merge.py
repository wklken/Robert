"""Read registered operator dependency replies without importing remote work."""

from sqlalchemy import func, select
from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables


def registrations(conn, repo_id, *, work_item_id=None):
    event, item = tables["work_item_events"], tables["work_items"]
    response = event.alias("dependency_response")
    question_id = func.json_extract(event.c.metadata_json, "$.question_id")
    resolved = select(1).where(response.c.resolves_event_id == question_id).exists()
    task = tables["tasks"]
    current = (
        select(1)
        .where(
            task.c.task_id == func.json_extract(event.c.metadata_json, "$.dependency.task_id"),
            task.c.updated_at == func.json_extract(event.c.metadata_json, "$.dependency.task_updated_at"),
            task.c.lifecycle == "waiting_for_user",
        )
        .exists()
    )
    query = (
        select(event)
        .select_from(event.join(item, item.c.work_item_id == event.c.work_item_id))
        .where(
            item.c.repo_id == repo_id,
            event.c.event_type == "dependency_merge_requested",
            item.c.canceled_at.is_(None),
            item.c.completed_at.is_(None),
            item.c.version == func.json_extract(event.c.metadata_json, "$.expected_version"),
            ~resolved,
            current,
        )
        .order_by(event.c.created_at, event.c.event_id)
    )
    if work_item_id is not None:
        query = query.where(event.c.work_item_id == work_item_id).limit(1)
    return statements.read(conn, query)


def registration(conn, work_item_id, event_id):
    event = tables["work_item_events"]
    return statements.read(
        conn,
        select(event).where(
            event.c.work_item_id == work_item_id,
            event.c.event_id == event_id,
            event.c.event_type == "dependency_merge_requested",
        ),
    )


def repo_name(conn, repo_id):
    return statements.read(conn, select(tables["repos"].c.full_name).where(tables["repos"].c.repo_id == repo_id))

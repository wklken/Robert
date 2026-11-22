"""Durable proposal comment receipts and unreported work-item milestones."""

from sqlalchemy import Text, case, cast, func, literal, or_, select

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries.proposals import same_task_lineage


CHANNEL = "proposal-comment"
TASK_EVENTS = ("operator_question", "execution_failed", "completed")
PR_EVENTS = ("pr_merged", "unmerged_pr_closed")


def pending(conn, now, limit=10):
    notice = tables["notifications"]
    retry_at = func.json_extract(notice.c.metadata_json, "$.delivery.next_retry_at")
    query = (
        select(notice)
        .where(
            notice.c.channel == CHANNEL,
            notice.c.status == "pending",
            or_(retry_at.is_(None), func.julianday(retry_at) <= func.julianday(now)),
        )
        .order_by(notice.c.created_at, notice.c.notification_id)
        .limit(limit)
    )
    return [dict(row) for row in statements.read(conn, query)]


def unreported(conn, repo_ids, projects, *, limit=20):
    proposal, revision, notice, event = (
        tables[name]
        for name in (
            "proposals",
            "proposal_revisions",
            "notifications",
            "work_item_events",
        )
    )
    source = proposal.join(
        revision, (proposal.c.proposal_id == revision.c.proposal_id) & (proposal.c.revision == revision.c.revision)
    )
    provider = func.json_extract(revision.c.source_json, "$.provider")
    scope = or_(
        (provider == "github") & proposal.c.repo_id.in_(repo_ids),
        (provider == "tapd") & func.json_extract(revision.c.source_json, "$.project_key").in_(projects),
    )
    accepted_id = literal("proposal-comment:") + proposal.c.proposal_id + literal(":admitted")
    accepted = (
        select(
            accepted_id.label("notification_id"),
            proposal.c.proposal_id,
            proposal.c.revision,
            revision.c.source_json,
            literal("admitted").label("event_type"),
            literal("{}").label("event_json"),
            proposal.c.updated_at.label("occurred_at"),
        )
        .select_from(source)
        .where(
            proposal.c.status == "admitted", scope, ~select(1).where(notice.c.notification_id == accepted_id).exists()
        )
    )
    task = tables["tasks"]
    root = task.alias("proposal_root")
    action = tables["github_actions"]
    bound_task = (
        select(task.c.task_id)
        .select_from(root.join(task, same_task_lineage(task, root)))
        .where(root.c.task_id == proposal.c.task_id)
        .correlate(proposal)
    )
    event_task = func.json_extract(event.c.metadata_json, "$.task_id")
    pr_for_event = (
        literal("https://github.com/")
        + func.json_extract(revision.c.source_json, "$.repository")
        + literal("/pull/")
        + cast(func.json_extract(event.c.metadata_json, "$.number"), Text)
    )
    bound_pr = (
        select(action.c.action_id)
        .where(
            action.c.task_id.in_(bound_task),
            action.c.target_url == pr_for_event,
            action.c.action_type.in_(("open_pr", "push_existing_pr")),
            action.c.publish_status == "published",
        )
        .correlate(proposal, revision, event)
        .exists()
    )
    progress_id = literal("proposal-comment:") + proposal.c.proposal_id + literal(":") + event.c.event_id
    progress = (
        select(
            progress_id.label("notification_id"),
            proposal.c.proposal_id,
            proposal.c.revision,
            revision.c.source_json,
            event.c.event_type,
            event.c.metadata_json.label("event_json"),
            event.c.created_at.label("occurred_at"),
        )
        .select_from(source.join(event, event.c.work_item_id == proposal.c.work_item_id))
        .where(
            proposal.c.status == "admitted",
            scope,
            event.c.created_at >= proposal.c.updated_at,
            or_(
                event.c.event_type.in_(TASK_EVENTS) & event_task.in_(bound_task),
                event.c.event_type.in_(PR_EVENTS) & bound_pr,
            ),
            ~select(1).where(notice.c.notification_id == progress_id).exists(),
        )
    )
    opened_id = literal("proposal-comment:") + proposal.c.proposal_id + literal(":") + action.c.action_id
    opened = (
        select(
            opened_id.label("notification_id"),
            proposal.c.proposal_id,
            proposal.c.revision,
            revision.c.source_json,
            literal("pr_opened").label("event_type"),
            func.json_object("url", action.c.target_url).label("event_json"),
            action.c.created_at.label("occurred_at"),
        )
        .select_from(source.join(action, action.c.task_id.in_(bound_task)))
        .where(
            proposal.c.status == "admitted",
            scope,
            action.c.action_type == "open_pr",
            action.c.publish_status == "published",
            action.c.target_url.is_not(None),
            ~select(1).where(notice.c.notification_id == opened_id).exists(),
        )
    )
    query = accepted.union_all(progress, opened).order_by("occurred_at", "notification_id").limit(limit)
    return [dict(row) for row in statements.read(conn, query)]


def status(conn, proposal_id):
    notice = tables["notifications"]
    query = (
        select(notice.c.notification_type, notice.c.status, notice.c.metadata_json)
        .where(
            notice.c.channel == CHANNEL,
            func.json_extract(notice.c.metadata_json, "$.proposal_id") == proposal_id,
        )
        .order_by(notice.c.created_at, notice.c.notification_id)
    )
    return [dict(row) for row in statements.read(conn, query)]


def pull_request(conn, root_task_id):
    action, task, source, stream = (
        tables[name] for name in ("github_actions", "tasks", "github_sources", "workstreams")
    )
    state = case(
        ((source.c.state == "closed") & (stream.c.lifecycle == "completed"), "merged"),
        (source.c.state == "closed", "closed"),
        else_="open",
    )
    root = task.alias("proposal_root")
    query = (
        select(action.c.target_url.label("url"), state.label("state"))
        .select_from(
            root.join(task, same_task_lineage(task, root))
            .join(action, action.c.task_id == task.c.task_id)
            .outerjoin(source, (source.c.html_url == action.c.target_url) & (source.c.source_type == "pull_request"))
            .outerjoin(stream, stream.c.primary_source_id == source.c.source_id)
        )
        .where(
            root.c.task_id == root_task_id,
            action.c.action_type.in_(("open_pr", "push_existing_pr")),
            action.c.publish_status == "published",
        )
        .order_by(action.c.created_at.desc(), action.c.action_id.desc())
        .limit(1)
    )
    rows = statements.read(conn, query)
    return dict(rows[0]) if rows else None

"""Materialized repair scopes, budget history and bound attempts."""

from sqlalchemy import func, select

from robert_agent.persistence.expressions import valid_json_or_empty
from robert_agent.persistence.queries import source_pr_lineage
from robert_agent.persistence.queries.source_sessions import blockers_for_source
from robert_agent.persistence.rounds.source_evidence import json_object

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def episode_attempt_count(episode):
    cycles, claims = tables["repair_cycles"], tables["repair_cycle_claims"]
    legacy = (
        select(cycles.c.legacy_attempt_count)
        .where(cycles.c.episode_id == episode.episode_id)
        .correlate_except(cycles)
        .scalar_subquery()
    )
    charged = (
        select(func.count()).where(claims.c.episode_id == episode.episode_id).correlate_except(claims).scalar_subquery()
    )
    return func.coalesce(legacy, 0) + charged


def load_cycle(conn, cycle_id):
    cycle = tables["repair_cycles"]
    return records.first(statements.read(conn, select(cycle).where(cycle.c.cycle_id == cycle_id)))


def current_native_cycle(conn, source_id, pr_source_id):
    cycle = tables["repair_cycles"]
    return records.first(
        statements.read(
            conn,
            select(cycle).where(
                cycle.c.source_id == source_id,
                cycle.c.pr_source_id == pr_source_id,
                cycle.c.status.in_(("active", "paused")),
            ),
        )
    )


def cycle_claims(conn, cycle_id):
    claim = tables["repair_cycle_claims"]
    return statements.read(
        conn, select(claim).where(claim.c.cycle_id == cycle_id).order_by(claim.c.created_at, claim.c.request_key)
    )


def cycle_attempts(conn, cycle_id):
    claim, attempt, usage = (tables[name] for name in ("repair_cycle_claims", "attempts", "repair_cycle_usage"))
    claimed_task = select(claim.c.task_id).where(claim.c.cycle_id == cycle_id)
    return statements.read(
        conn,
        select(attempt.c.attempt_id, attempt.c.status, usage.c.tokens, usage.c.cost_usd, usage.c.observed_at)
        .select_from(
            attempt.outerjoin(usage, (usage.c.attempt_id == attempt.c.attempt_id) & (usage.c.cycle_id == cycle_id))
        )
        .where(attempt.c.task_id.in_(claimed_task)),
    )


def latest_native_cycle(conn, source_id, pr_source_id):
    cycle = tables["repair_cycles"]
    return records.first(
        statements.read(
            conn,
            select(cycle)
            .where(cycle.c.source_id == source_id, cycle.c.pr_source_id == pr_source_id)
            .order_by(func.julianday(cycle.c.created_at).desc(), cycle.c.cycle_id.desc())
            .limit(1),
        )
    )


def native_pr_scope(conn, *, source_id, pr_source_id):
    """Resolve the original canonical creation receipt for this exact derived PR."""
    matches = source_pr_lineage.workstream_for_pr(conn, pr_source_id)
    if len(matches) != 1:
        return None
    stream = matches[0]
    metadata = json_object(stream["metadata_json"]) or {}
    creation = metadata.get("tapd_creation")
    if not isinstance(creation, dict) or not isinstance(creation.get("action_id"), str):
        return None
    lineage = source_pr_lineage.confirmed_pr_lineage(
        conn,
        source_id=source_id,
        repo_id=stream["repo_id"],
        workstream_id=stream["workstream_id"],
        action_id=creation["action_id"],
    )
    if lineage is None or lineage.pr_source_id != pr_source_id:
        return None
    repo = tables["repos"]
    allowed = statements.read(
        conn,
        select(repo.c.repo_id).where(
            repo.c.repo_id == lineage.repo_id,
            repo.c.full_name == lineage.repository,
            func.lower(repo.c.github_account) == lineage.author.casefold(),
            repo.c.enabled == 1,
        ),
    )
    return lineage if allowed else None


def native_episode_for_task(conn, *, round_id, request_id, task_id, episode_id, workstream_id, repo_id):
    attachment, task, episode = (tables[name] for name in ("source_round_tasks", "tasks", "pr_remediation_episodes"))
    return records.first(
        statements.read(
            conn,
            select(episode)
            .select_from(
                attachment.join(task, task.c.task_id == attachment.c.task_id).join(
                    episode, episode.c.workstream_id == task.c.workstream_id
                )
            )
            .where(
                attachment.c.round_id == round_id,
                attachment.c.request_id == request_id,
                task.c.task_id == task_id,
                attachment.c.capability == "development",
                attachment.c.repo_id == repo_id,
                episode.c.episode_id == episode_id,
                task.c.workstream_id == workstream_id,
                func.json_extract(valid_json_or_empty(task.c.metadata_json), "$.remediation_episode_id")
                == episode.c.episode_id,
                task.c.task_kind
                == func.iif(episode.c.episode_kind == "ci", "ci_remediation", "merge_conflict_remediation"),
            ),
        )
    )


def claim_by_request(conn, request_key):
    claim = tables["repair_cycle_claims"]
    return records.first(
        statements.read(conn, select(claim).where(claim.c.request_key == request_key, claim.c.round_id.is_not(None)))
    )


def source_repair_unsettled(conn, source_id):
    """Healthy closure requires the source's current and historical children to settle."""
    return bool(blockers_for_source(conn, source_id, limit=1))

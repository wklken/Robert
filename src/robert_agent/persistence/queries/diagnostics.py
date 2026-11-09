"""Select bounded no-action diagnostic details with recovery fences."""

from sqlalchemy import select, func, or_, case

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import sql_constant, valid_json_or_empty


RUN_ACTION_COUNTERS = (
    "dispatch_count",
    "prompt_count",
    "audited_result_count",
    "supervised_count",
    "reconciled_source_count",
    "current_state_closed_skip_count",
)
REPO_ACTION_COUNTERS = (
    "supervised_count",
    "failed_timeout_recovered_count",
    "prompt_count",
    "dispatch_queue_count",
    "audited_result_count",
    "publish_pending_count",
    "published_count",
    "publish_skipped_count",
    "publish_failed_count",
    "finalized_task_count",
    "reconciled_source_count",
    "current_state_closed_skip_count",
)


def _zero(document, key):
    path = sql_constant("$." + key)
    return func.json_type(document, path).in_(("integer", "real")) & (func.json_extract(document, path) == 0)


def compactable_runs(conn, batch_size, *, excluded_run_ids=()):
    runs, steps, repo_steps, leases, wakes = (
        tables[name] for name in ("agent_runs", "run_steps", "run_repo_steps", "leases", "wakeups")
    )
    summary = valid_json_or_empty(runs.c.summary_json)
    repos = func.json_each(summary, sql_constant("$.repo_summaries")).table_valued("value", "type")
    repo = case((repos.c.type == "object", repos.c.value), else_=sql_constant("{}"))
    repo_ok = (func.json_extract(repo, sql_constant("$.status")) == "completed") & (
        func.json_type(repo, sql_constant("$.failure_status")) == "null"
    )
    for key in REPO_ACTION_COUNTERS:
        repo_ok &= _zero(repo, key)
    for key in ("raw_event_count", "decision_count"):
        path = sql_constant("$." + key)
        repo_ok &= func.json_type(repo, path).in_(("integer", "real")) & (func.json_extract(repo, path) >= 0)
    # Unknown or missing fields are retained, even though SQL NULL comparisons
    # would otherwise silently pass NOT EXISTS.
    invalid_repo = select(1).select_from(repos).where(func.coalesce(repo_ok, False).is_(False))
    recent = select(runs.c.run_id).order_by(runs.c.started_at.desc(), runs.c.run_id.desc()).limit(200)
    route_run_ids = []
    for repository in statements.read(conn, select(tables["repos"].c.repo_id)):
        latest_time = (
            select(func.max(repo_steps.c.finished_at))
            .where(
                repo_steps.c.repo_id == repository[0],
                repo_steps.c.step_key == "route",
                repo_steps.c.status == "succeeded",
            )
            .scalar_subquery()
        )
        route_run_ids.extend(
            row[0]
            for row in statements.read(
                conn,
                select(repo_steps.c.run_id).where(
                    repo_steps.c.repo_id == repository[0],
                    repo_steps.c.step_key == "route",
                    repo_steps.c.status == "succeeded",
                    repo_steps.c.finished_at == latest_time,
                ),
            )
        )
    predicates = [
        runs.c.status == "completed",
        runs.c.finished_at.is_not(None),
        runs.c.error_json.is_(None),
        ~runs.c.run_id.in_(excluded_run_ids),
        func.json_extract(summary, sql_constant("$.overall_status")) == "completed",
        func.json_extract(summary, sql_constant("$.diagnostics.detail_status")).is_(None),
        or_(
            func.json_type(summary, sql_constant("$.diagnostics")).is_(None),
            func.json_type(summary, sql_constant("$.diagnostics")) == "object",
        ),
        func.json_type(summary, sql_constant("$.repo_summaries")) == "array",
        func.json_array_length(summary, sql_constant("$.repo_summaries")) > 0,
        ~invalid_repo.exists(),
        ~runs.c.run_id.in_(recent),
        ~runs.c.run_id.in_(route_run_ids),
        ~select(1)
        .select_from(wakes)
        .where(or_(wakes.c.source_run_id == runs.c.run_id, wakes.c.consumed_run_id == runs.c.run_id))
        .correlate(runs)
        .exists(),
        ~select(1)
        .select_from(leases)
        .where(leases.c.owner_id == runs.c.run_id, leases.c.status == "active")
        .correlate(runs)
        .exists(),
        select(1).select_from(repo_steps).where(repo_steps.c.run_id == runs.c.run_id).correlate(runs).exists(),
        func.json_type(summary, sql_constant("$.publish_result.ok")) == "true",
    ]
    predicates.extend(_zero(summary, key) for key in RUN_ACTION_COUNTERS)
    predicates.extend(
        _zero(summary, "publish_result." + key)
        for key in ("pending_count", "published_count", "skipped_count", "failed_count", "finalized_task_count")
    )
    for table in (steps, repo_steps):
        predicates.append(
            ~select(1)
            .select_from(table)
            .where(
                table.c.run_id == runs.c.run_id,
                or_(table.c.error_json.is_not(None), table.c.status.in_(("pending", "running", "failed"))),
            )
            .correlate(runs)
            .exists()
        )
    query = (
        select(runs.c.run_id)
        .where(*predicates)
        .order_by(func.julianday(runs.c.finished_at), runs.c.run_id)
        .limit(batch_size)
    )
    return [row[0] for row in statements.read(conn, query)]

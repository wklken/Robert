from robert_agent.persistence.expressions import sql_constant, valid_json_or_empty
from sqlalchemy import select, update, delete, case, func

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def prune_rows(conn, runs, events, leases, *, apply):
    groups = (
        ("run_steps", "run_id", runs),
        ("run_repo_steps", "run_id", runs),
        ("agent_runs", "run_id", runs),
        ("daemon_events", "daemon_event_id", events),
        ("leases", "lease_id", leases),
    )
    counts = {}
    for name, key, identifiers in groups:
        table = tables[name]
        predicate = table.c[key].in_(identifiers)
        counts[name] = (
            statements.read(conn, select(func.count()).select_from(table).where(predicate))[0][0] if identifiers else 0
        )
        if apply and identifiers:
            statements.write(conn, delete(table).where(predicate))
    return counts


def interrupt_steps(conn, run_id, now, error_json):
    for name in ("run_steps", "run_repo_steps"):
        table = tables[name]
        statements.write(
            conn,
            update(table)
            .where(
                table.c.run_id == run_id,
                table.c.status.in_((sql_constant("running"), sql_constant("pending"))),
            )
            .values(
                status=case(
                    (table.c.status == sql_constant("running"), "failed"),
                    else_="skipped",
                ),
                finished_at=now,
                error_json=error_json,
            ),
        )


def compact_run_details(conn, run_ids, *, apply, excluded_lease_ids=()):
    """Keep canonical summaries while atomically removing no-action detail."""
    counts = {"run_steps": 0, "run_repo_steps": 0, "leases": 0}
    for name in counts:
        table = tables[name]
        predicate = table.c.owner_id.in_(run_ids) if name == "leases" else table.c.run_id.in_(run_ids)
        if name == "leases":
            predicate &= (table.c.resource_type == "agent_run") & (table.c.status == "released")
            predicate &= ~table.c.lease_id.in_(excluded_lease_ids)
        counts[name] = statements.read(conn, select(func.count()).select_from(table).where(predicate))[0][0]
        if apply and run_ids:
            statements.write(conn, delete(table).where(predicate))
    if apply and run_ids:
        runs = tables["agent_runs"]
        statements.write(
            conn,
            update(runs)
            .where(runs.c.run_id.in_(run_ids))
            .values(
                summary_json=func.json_set(
                    runs.c.summary_json, sql_constant("$.diagnostics.detail_status"), "compacted_no_actions"
                )
            ),
        )
    return counts


def reference_pipeline_summary(conn, run_id):
    """Only compact a pipeline output matching its already-persisted summary."""
    runs, steps = tables["agent_runs"], tables["run_steps"]
    summary = select(runs.c.summary_json).where(runs.c.run_id == run_id).scalar_subquery()
    output = valid_json_or_empty(steps.c.output_json)
    statements.write(
        conn,
        update(steps)
        .where(
            steps.c.run_id == run_id,
            steps.c.step_key == "repo_pipelines",
            func.json_valid(output),
            func.json_type(output, sql_constant("$.repo_summaries")) == "array",
            func.json_extract(output, sql_constant("$.repo_summaries"))
            == func.json_extract(valid_json_or_empty(summary), sql_constant("$.repo_summaries")),
        )
        .values(
            output_json=func.json_set(
                func.json_remove(output, sql_constant("$.repo_summaries")),
                sql_constant("$.repo_summaries_ref"),
                "agent_runs.summary_json.repo_summaries",
            )
        ),
    )

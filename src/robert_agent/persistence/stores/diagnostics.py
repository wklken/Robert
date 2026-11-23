from robert_agent.persistence.expressions import sql_constant, valid_json_or_empty
from sqlalchemy import select, update, delete, case, func

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements
from robert_agent.persistence.errors import IntegrityError
from robert_agent.persistence.queries.run_summaries import resolved_summary
from sqlalchemy.dialects.sqlite import insert
import hashlib
import json


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


def reference_pipeline_summary(conn, run_id, *, terminal_only=False):
    """Only compact a pipeline output matching its already-persisted summary."""
    runs, steps = tables["agent_runs"], tables["run_steps"]
    summary = select(resolved_summary(conn, runs)).where(runs.c.run_id == run_id).scalar_subquery()
    output = valid_json_or_empty(steps.c.output_json)
    predicate = [
        steps.c.run_id == run_id,
        steps.c.step_key == "repo_pipelines",
        func.json_valid(steps.c.output_json),
        func.json_valid(summary),
        func.json_type(output, sql_constant("$.repo_summaries_ref")).is_(None),
        func.json_type(output, sql_constant("$.repo_summaries")) == "array",
        func.json_extract(output, sql_constant("$.repo_summaries"))
        == func.json_extract(valid_json_or_empty(summary), sql_constant("$.repo_summaries")),
    ]
    if terminal_only:
        predicate.append(steps.c.status.in_(("succeeded", "failed", "skipped")))
    return statements.write(
        conn,
        update(steps)
        .where(*predicate)
        .values(
            output_json=func.json_set(
                func.json_remove(output, sql_constant("$.repo_summaries")),
                sql_constant("$.repo_summaries_ref"),
                "agent_runs.summary_json.repo_summaries",
            )
        ),
    )


def deduplicate_summaries(conn, candidates, *, apply):
    runs, refs, summaries = (
        tables[name] for name in ("agent_runs", "agent_run_summary_refs", "diagnostic_run_summaries")
    )
    shared_ids = set()
    for run_id, encoded in candidates:
        canonical = json.dumps(json.loads(encoded), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        summary_id = hashlib.sha256(canonical.encode()).hexdigest()
        shared_ids.add(summary_id)
        if not apply:
            continue
        statements.write(
            conn,
            insert(summaries)
            .values(summary_id=summary_id, summary_json=canonical)
            .on_conflict_do_nothing(index_elements=[summaries.c.summary_id]),
        )
        stored = statements.read(conn, select(summaries.c.summary_json).where(summaries.c.summary_id == summary_id))[0][
            0
        ]
        if stored != canonical:
            raise IntegrityError("Shared diagnostic summary identity collision")
        statements.write(conn, insert(refs).values(run_id=run_id, summary_id=summary_id))
        statements.write(
            conn,
            update(runs)
            .where(runs.c.run_id == run_id)
            .values(
                summary_json='{"summary_ref":"diagnostic_run_summaries","diagnostics":{"detail_status":"compacted_no_actions"}}'
            ),
        )
    return shared_ids


def reference_historical_steps(conn, run_ids, *, apply):
    runs, steps = tables["agent_runs"], tables["run_steps"]
    count = 0
    for run_id in run_ids:
        summary = select(resolved_summary(conn, runs)).where(runs.c.run_id == run_id).scalar_subquery()
        predicate = (
            (steps.c.run_id == run_id)
            & (steps.c.step_key == "summarize")
            & func.json_valid(steps.c.output_json)
            & func.json_valid(summary)
            & (func.json_type(valid_json_or_empty(summary)) == "object")
            & (func.json(valid_json_or_empty(steps.c.output_json)) == func.json(valid_json_or_empty(summary)))
            & (steps.c.status.in_(("succeeded", "failed", "skipped")))
        )
        count += statements.read(conn, select(func.count()).select_from(steps).where(predicate))[0][0]
        if apply:
            statements.write(
                conn, update(steps).where(predicate).values(output_json='{"summary_ref":"agent_runs.summary_json"}')
            )
            count += reference_pipeline_summary(conn, run_id, terminal_only=True)
        else:
            output = valid_json_or_empty(steps.c.output_json)
            count += statements.read(
                conn,
                select(func.count())
                .select_from(steps)
                .where(
                    steps.c.run_id == run_id,
                    steps.c.step_key == "repo_pipelines",
                    steps.c.status.in_(("succeeded", "failed", "skipped")),
                    func.json_valid(steps.c.output_json),
                    func.json_valid(summary),
                    func.json_type(output, sql_constant("$.repo_summaries_ref")).is_(None),
                    func.json_type(output, sql_constant("$.repo_summaries")) == "array",
                    func.json_extract(output, sql_constant("$.repo_summaries"))
                    == func.json_extract(valid_json_or_empty(summary), sql_constant("$.repo_summaries")),
                ),
            )[0][0]
    return count


def prune_shared_summaries(conn, identifiers, *, apply):
    if apply and identifiers:
        table = tables["diagnostic_run_summaries"]
        statements.write(conn, delete(table).where(table.c.summary_id.in_(identifiers)))

from robert_agent.persistence.expressions import sql_constant
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

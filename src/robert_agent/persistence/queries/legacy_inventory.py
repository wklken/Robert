from robert_agent.persistence.queries import sqlite_master

from sqlalchemy import select, literal_column

from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements

TABLES = (
    "workflow_bindings",
    "workflow_outbox",
    "workflow_signal_receipts",
    "tasks",
    "attempts",
    "worker_results",
    "github_actions",
    "publication_intents",
    "github_events",
    "task_events",
    "workstreams",
    "pr_remediation_episodes",
    "wakeups",
    "leases",
    "daemon_runs",
    "agent_runs",
    "execution_subjects",
    "execution_pending_facts",
    "execution_fact_receipts",
    "notifications",
)


def rows(conn, name):
    if name not in TABLES:
        raise ValueError("unsupported migration inventory table")
    exists = sqlite_master.table_exists(conn, name)
    if not exists:
        return []
    return statements.read(conn, select(literal_column("*")).select_from(tables[name]))

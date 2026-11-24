"""Shared review/vote record access with fixed table and field contracts."""

import hashlib
import json

from sqlalchemy import select, update

from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables


def artifact_available(run):
    """A validated adoption retains the original invocation failure history."""
    if run.get("status") == "succeeded":
        return True
    receipt = (run.get("dispatch") or {}).get("artifact_adoption")
    if run.get("status") not in {"failed", "interrupted"} or not isinstance(receipt, dict):
        return False
    artifact = run.get("artifact")
    return bool(
        isinstance(artifact, dict)
        and artifact
        and receipt.get("run_id") == run.get("run_id")
        and receipt.get("prompt_sha256") == run.get("prompt_sha256")
        and receipt.get("original_status") == run.get("status")
        and receipt.get("original_error") == run.get("error")
        and receipt.get("artifact_sha256") == hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
    )


def round_table(name):
    if name not in {
        "review_sessions",
        "review_runs",
        "vote_sessions",
        "vote_runs",
        "pre_pr_review_sessions",
        "pre_pr_review_runs",
    }:
        raise ValueError("unknown model round table")
    return tables[name]


def session(conn, table_name, *, round_id=None, task_id=None):
    if not round_id and task_id is None:
        return None
    table = round_table(table_name)
    condition = table.c.round_id == round_id if round_id else table.c.task_id == task_id
    return records.first(statements.read(conn, select(table).where(condition).limit(1)))


def update_session(conn, table_name, round_id, fields):
    table = round_table(table_name)
    allowed = {
        "task_id",
        "status",
        "manifest_json",
        "report_path",
        "report_sha256",
        "error",
        "updated_at",
    }
    if table_name == "vote_sessions":
        allowed.add("request_json")
    if table_name == "pre_pr_review_sessions":
        allowed |= {"proof_json", "final_result_id", "snapshot_id", "request_json", "deadline_at", "next_retry_at"}
    if not set(fields) <= allowed:
        raise ValueError("invalid round session fields")
    return statements.write(conn, update(table).where(table.c.round_id == round_id).values(fields))


def update_run(conn, table_name, run_id, fields):
    table = round_table(table_name)
    # Caller-specific contracts constrain this further before this shared write.
    if not set(fields) <= set(table.c.keys()) - {"run_id", "round_id"}:
        raise ValueError("invalid round run fields")
    return statements.write(conn, update(table).where(table.c.run_id == run_id).values(fields))

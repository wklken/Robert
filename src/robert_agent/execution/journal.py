"""Persist controller run steps and deadline cleanup."""

from contextlib import closing
from contextlib import contextmanager
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
import json
import sqlite3
import time


SUMMARY_REFERENCE = {"summary_ref": "agent_runs.summary_json"}


def insert_run_step(conn, run_id, key, status="pending"):
    conn.execute(
        """
        INSERT INTO run_steps(step_id, run_id, step_key, status)
        VALUES (?, ?, ?, ?)
        """,
        (values.new_id("step"), run_id, key, status),
    )


_STEP_CLOCKS: dict[tuple[str, str | None, str], float] = {}


def _step_duration(identity, status, output):
    if status == "running":
        _STEP_CLOCKS.setdefault(identity, time.monotonic())
    if status in {"succeeded", "failed", "skipped"}:
        started = _STEP_CLOCKS.pop(identity, None)
        if started is not None:
            output = {
                **(output if isinstance(output, dict) else {}),
                "duration_seconds": max(0, time.monotonic() - started),
            }
    return output


def mark_run_step(conn, run_id, key, status, run_now=None, output=None, error=None):
    run_now = values.utc_now()
    output = _step_duration((run_id, None, key), status, output)
    if key == "summarize" and status == "succeeded" and isinstance(output, dict):
        output = SUMMARY_REFERENCE
    output_json = json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None
    error_json = json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None
    finished_at = run_now if status in {"succeeded", "failed", "skipped"} else None
    conn.execute(
        """
        UPDATE run_steps
        SET status = ?,
            started_at = COALESCE(started_at, ?),
            finished_at = ?,
            output_json = COALESCE(?, output_json),
            error_json = COALESCE(?, error_json)
        WHERE run_id = ?
          AND step_key = ?
        """,
        (status, run_now, finished_at, output_json, error_json, run_id, key),
    )


def _insert_repo_run_step(conn, run_id, repo_id, key, status="pending"):
    conn.execute(
        """
        INSERT OR IGNORE INTO run_repo_steps(step_id, run_id, repo_id, step_key, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (values.new_id("repo-step"), run_id, repo_id, key, status),
    )


def mark_repo_run_step(conn, run_id, repo_id, key, status, run_now=None, output=None, error=None):
    run_now = values.utc_now()
    output = _step_duration((run_id, repo_id, key), status, output)
    finished_at = run_now if status in {"succeeded", "failed", "skipped"} else None
    output_json = json.dumps(output, ensure_ascii=False, sort_keys=True) if output is not None else None
    error_json = json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None
    conn.execute(
        """
        UPDATE run_repo_steps
        SET status = ?,
            started_at = COALESCE(started_at, ?),
            finished_at = COALESCE(?, finished_at),
            output_json = COALESCE(?, output_json),
            error_json = COALESCE(?, error_json)
        WHERE run_id = ?
          AND repo_id = ?
          AND step_key = ?
        """,
        (status, run_now, finished_at, output_json, error_json, run_id, repo_id, key),
    )


REPO_STEP_ORDER = [
    "acquire_lease",
    "supervise",
    "audit_results",
    "publish_actions",
    "reconcile",
    "discover",
    "authorize",
    "route",
]


def init_repo_steps(conn, run_id, repo_id):
    for step in REPO_STEP_ORDER:
        _insert_repo_run_step(conn, run_id, repo_id, step)


def aggregate_repo_step_status(conn, run_id, step_key):
    rows = conn.execute(
        """
        SELECT status
        FROM run_repo_steps
        WHERE run_id = ?
          AND step_key = ?
        """,
        (run_id, step_key),
    ).fetchall()
    statuses = [row[0] for row in rows]
    if not statuses:
        return "pending"
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if "succeeded" in statuses:
        return "succeeded"
    if "skipped" in statuses:
        return "skipped"
    return "pending"


@contextmanager
def run_transaction(db_path, run_id):
    """Persist invocation cleanup even when its domain transaction rolls back."""
    with closing(connect(db_path)) as conn:
        try:
            with conn:
                yield conn
        except ControlDeadlineExceeded:
            now = values.utc_now()
            error = json.dumps(
                {
                    "status": "execution_deadline",
                    "safe_error": "Control command exceeded its execution deadline.",
                }
            )
            with conn:
                interrupt_run(conn, run_id, now, error)
            raise


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def start_run(conn, run_id, started_at, config_path, summary_json, *, dry_run=False):
    conn.execute(
        """INSERT INTO agent_runs(run_id, status, started_at, config_path, dry_run, summary_json)
           VALUES (?, 'running', ?, ?, ?, ?)""",
        (run_id, started_at, str(config_path), int(bool(dry_run)), summary_json),
    )


def finish_run(conn, run_id, status, finished_at, summary, *, error=None, sort_keys=True):
    summary_json = json.dumps(summary, sort_keys=sort_keys)
    if error is None:
        conn.execute(
            """UPDATE agent_runs SET status=?,finished_at=?,summary_json=? WHERE run_id=?""",
            (status, finished_at, summary_json, run_id),
        )
    else:
        conn.execute(
            """UPDATE agent_runs SET status=?,finished_at=?,summary_json=?,error_json=? WHERE run_id=?""",
            (
                status,
                finished_at,
                summary_json,
                json.dumps(error, ensure_ascii=False, sort_keys=True),
                run_id,
            ),
        )


def running_repo_step(conn, run_id, repo_id):
    row = conn.execute(
        "SELECT step_key FROM run_repo_steps WHERE run_id=? AND repo_id=? AND status='running'",
        (run_id, repo_id),
    ).fetchone()
    return row[0] if row else None


def mark_repo_failure(conn, run_id, repo_id, failed_step, run_now, error):
    for step in REPO_STEP_ORDER:
        row = conn.execute(
            "SELECT status FROM run_repo_steps WHERE run_id=? AND repo_id=? AND step_key=?",
            (run_id, repo_id, step),
        ).fetchone()
        if row and (row[0] in {"pending", "running"} or step == failed_step):
            mark_repo_run_step(
                conn,
                run_id,
                repo_id,
                step,
                "failed" if step == failed_step else "skipped",
                run_now,
                error=error,
            )


def summarize_repositories(
    conn,
    run_id,
    run_now,
    repo_summaries,
    prompt_count,
    queued_count,
    *,
    dry_run,
    skip_publish,
):
    aggregate_counts = {
        "supervised_count": sum(item["supervised_count"] for item in repo_summaries),
        "failed_timeout_recovered_count": sum(item["failed_timeout_recovered_count"] for item in repo_summaries),
        "audited_result_count": sum(item["audited_result_count"] for item in repo_summaries),
        "reconciled_source_count": sum(item["reconciled_source_count"] for item in repo_summaries),
        "raw_event_count": sum(item["raw_event_count"] for item in repo_summaries),
        "decision_count": sum(item["decision_count"] for item in repo_summaries),
        "current_state_closed_skip_count": sum(item["current_state_closed_skip_count"] for item in repo_summaries),
        "publish_pending_count": sum(item["publish_pending_count"] for item in repo_summaries),
        "published_count": sum(item["published_count"] for item in repo_summaries),
        "publish_skipped_count": sum(item["publish_skipped_count"] for item in repo_summaries),
        "publish_failed_count": sum(item["publish_failed_count"] for item in repo_summaries),
        "finalized_task_count": sum(item["finalized_task_count"] for item in repo_summaries),
    }
    publish_result = {
        "ok": aggregate_counts["publish_failed_count"] == 0,
        "status": (
            "skipped"
            if skip_publish
            else (
                "publish_failed" if aggregate_counts["publish_failed_count"] else "dry_run" if dry_run else "published"
            )
        ),
        "pending_count": aggregate_counts["publish_pending_count"],
        "published_count": aggregate_counts["published_count"],
        "skipped_count": aggregate_counts["publish_skipped_count"],
        "failed_count": aggregate_counts["publish_failed_count"],
        "finalized_task_count": aggregate_counts["finalized_task_count"],
    }
    if skip_publish:
        publish_result["reason"] = "skip_publish"

    step_outputs = {
        "acquire_lease": {
            "repo_count": len(repo_summaries),
            "acquired_count": sum(1 for item in repo_summaries if item["lease_status"] == "acquired"),
            "skipped_active_lease_count": sum(
                1 for item in repo_summaries if item["lease_status"] == "skipped_active_lease"
            ),
        },
        "supervise": {
            "failed_timeout_recovered_count": aggregate_counts["failed_timeout_recovered_count"],
            "supervised_count": aggregate_counts["supervised_count"],
        },
        "audit_results": {
            "audited_result_count": aggregate_counts["audited_result_count"],
        },
        "publish_actions": publish_result,
        "reconcile": {
            "reconciled_source_count": aggregate_counts["reconciled_source_count"],
        },
        "discover": {
            "raw_event_count": aggregate_counts["raw_event_count"],
        },
        "authorize": {
            "decision_count": aggregate_counts["decision_count"],
        },
        "route": {
            "prompt_count": prompt_count,
            "dispatch_queue_count": queued_count,
            "reconciled_source_count": aggregate_counts["reconciled_source_count"],
            "current_state_closed_skip_count": aggregate_counts["current_state_closed_skip_count"],
        },
    }

    for step in REPO_STEP_ORDER:
        failed_summary = next(
            (item for item in repo_summaries if item["status"] == "failed" and item.get("safe_error")),
            None,
        )
        error = None
        if aggregate_repo_step_status(conn, run_id, step) == "failed" and failed_summary:
            error = {
                "status": failed_summary.get("failure_status", "failed"),
                "safe_error": failed_summary["safe_error"],
                "repo_id": failed_summary["repo_id"],
                "full_name": failed_summary["full_name"],
            }
        mark_run_step(
            conn,
            run_id,
            step,
            aggregate_repo_step_status(conn, run_id, step),
            run_now,
            output=step_outputs.get(step),
            error=error,
        )

    failed_repos = [item for item in repo_summaries if item["status"] == "failed"]
    succeeded_repos = [item for item in repo_summaries if item["status"] in {"completed", "skipped_active_lease"}]
    if repo_summaries and all(item["status"] == "skipped_active_lease" for item in repo_summaries):
        overall_status = "skipped_active_lease"
    elif failed_repos and succeeded_repos:
        overall_status = "partial_failure"
    elif failed_repos:
        overall_status = "failed"
    else:
        overall_status = "completed"

    repo_pipeline_status = (
        "skipped" if overall_status == "skipped_active_lease" else ("failed" if failed_repos else "succeeded")
    )
    mark_run_step(
        conn,
        run_id,
        "repo_pipelines",
        repo_pipeline_status,
        run_now,
        output={"repo_summaries": repo_summaries, "overall_status": overall_status},
    )
    return aggregate_counts, publish_result, overall_status


def dispatch_failure_for(dispatch_results, dispatch_error):
    return dispatch_error or next(
        (result for result in dispatch_results if not result.get("ok", True) or result.get("status") == "failed"),
        None,
    )


def finish_dispatch(
    conn,
    run_id,
    overall_status,
    repo_summaries,
    prompt_count,
    dispatch_results,
    dispatch_error,
    aggregate_counts,
    publish_result,
):
    finished_at = values.utc_now()
    failure = dispatch_failure_for(dispatch_results, dispatch_error)
    dispatch_count = len(dispatch_results)
    summary = {
        "overall_status": "failed" if failure else overall_status,
        "repo_summaries": repo_summaries,
        "prompt_count": prompt_count,
        "dispatch_count": dispatch_count,
        "audited_result_count": aggregate_counts["audited_result_count"],
        "publish_result": publish_result,
        "reconciled_source_count": aggregate_counts["reconciled_source_count"],
        "current_state_closed_skip_count": aggregate_counts["current_state_closed_skip_count"],
        "supervised_count": aggregate_counts["supervised_count"],
    }
    if failure:
        mark_run_step(
            conn,
            run_id,
            "dispatch",
            "failed",
            finished_at,
            output={"dispatch_count": dispatch_count},
            error=failure,
        )
        mark_run_step(
            conn,
            run_id,
            "summarize",
            "skipped",
            finished_at,
            output={"reason": failure.get("status", "failed_dispatch")},
        )
        finish_run(conn, run_id, "failed", finished_at, summary, error=failure)
        return
    mark_run_step(
        conn,
        run_id,
        "dispatch",
        "succeeded",
        finished_at,
        output={"dispatch_count": dispatch_count},
    )
    mark_run_step(conn, run_id, "summarize", "succeeded", finished_at, output=summary)
    if overall_status not in {"failed", "partial_failure"}:
        finish_run(conn, run_id, "completed", finished_at, summary)
        return
    first_repo_error = next(
        (
            {
                "status": item.get("failure_status", "failed"),
                "safe_error": item["safe_error"],
                "repo_id": item["repo_id"],
                "full_name": item["full_name"],
            }
            for item in repo_summaries
            if item["status"] == "failed"
        ),
        None,
    )
    finish_run(
        conn,
        run_id,
        "failed",
        finished_at,
        summary,
        error=first_repo_error or publish_result,
    )


def interrupt_run(conn, run_id, now, error_json):
    conn.execute(
        """UPDATE leases SET status='released',released_at=?,heartbeat_at=?
        WHERE resource_type='agent_run' AND owner_id=? AND status='active'""",
        (now, now, run_id),
    )
    for table in ("run_steps", "run_repo_steps"):
        conn.execute(
            f"""UPDATE {table}
            SET status=CASE WHEN status='running' THEN 'failed' ELSE 'skipped' END,
                finished_at=?,error_json=?
            WHERE run_id=? AND status IN ('running','pending')""",
            (now, error_json, run_id),
        )
    conn.execute(
        """UPDATE agent_runs SET status='failed',finished_at=?,error_json=?
        WHERE run_id=? AND status='running'""",
        (now, error_json, run_id),
    )


def finish_stopped_executor(conn, executor_id, now):
    error = json.dumps(
        {
            "status": "executor_interrupted",
            "safe_error": "Control executor stopped before recording its outcome.",
        }
    )
    run_ids = [
        row[0]
        for row in conn.execute(
            """SELECT run_id FROM agent_runs
        WHERE status='running' AND json_extract(summary_json,'$.executor_id')=?""",
            (executor_id,),
        )
    ]
    for run_id in run_ids:
        interrupt_run(conn, run_id, now, error)


def running_executors(conn):
    return conn.execute("""SELECT json_extract(summary_json,'$.executor_id'),
        json_extract(summary_json,'$.executor_identity') FROM agent_runs
        WHERE status='running' AND json_type(summary_json,'$.executor_identity')='object'""").fetchall()

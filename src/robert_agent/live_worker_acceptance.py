#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from robert_agent.common import emit
from robert_agent import run_once
from robert_agent import validate_config
from robert_agent.resource_files import resource


DEFAULT_WORKFLOW = resource("workflow.yml")


def _acceptance_id():
    return f"live-acceptance-{uuid4().hex[:12]}"


def _json_load(value):
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tail(path, limit=4000):
    if not path:
        return ""
    path = Path(path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _workspace_dir(workspace_dir):
    if workspace_dir:
        path = Path(workspace_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="dd-live-acceptance-")).resolve()


def _isolated_config(source_config, workspace_dir):
    config = validate_config.validate_config(source_config, skip_external=True)
    if not config["ok"]:
        return config
    workers = [
        {
            "name": worker["name"],
            "agent": worker["agent"],
            "command": worker["command"],
            "default_model": worker["default_model"],
            "default_effort": worker["default_effort"],
        }
        for worker in config["workers"]
    ]
    isolated = {
        "data_dir": str(workspace_dir / "data"),
        "database": "dd.sqlite3",
        "workers": workers,
        "route_worker_models": config.get("route_worker_models", {}),
        "max_concurrency": 1,
        "stale_after_minutes": config.get("stale_after_minutes", 20),
        "hard_timeout_minutes": config.get("hard_timeout_minutes", 90),
        "worker_startup_grace_seconds": config.get("worker_startup_grace_seconds", 300),
        "lease_ttl_minutes": config.get("lease_ttl_minutes", 9),
        "repos": config["repos"],
    }
    config_path = workspace_dir / "config.json"
    _write_json(config_path, isolated)
    return {
        "ok": True,
        "status": "prepared",
        "source_config": config,
        "config_path": str(config_path),
        "db_path": str(workspace_dir / "data" / "dd.sqlite3"),
    }


def _default_fixture(config, workspace_dir, issue_number=77):
    repo = config["repos"][0]
    actor = repo["trusted_actors"][0]
    github_account = repo["github_account"]
    full_name = repo["full_name"]
    event_id = _acceptance_id()
    fixture_path = workspace_dir / "fixture.json"
    _write_json(
        fixture_path,
        {
            "events": [
                {
                    "id": event_id,
                    "number": issue_number,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": actor,
                    "author_association": "OWNER",
                    "body": f"@{github_account} please analyze this controlled live acceptance event",
                    "intent": "analysis",
                    "url": f"https://github.com/{full_name}/issues/{issue_number}",
                }
            ]
        },
    )
    return fixture_path


def _latest_artifacts(conn, task_id):
    rows = conn.execute(
        """
        SELECT artifact_type, path
        FROM artifacts
        WHERE task_id = ?
          AND artifact_type IN ('prompt', 'worker_stdout', 'worker_stderr')
        ORDER BY created_at, artifact_id
        """,
        (task_id,),
    ).fetchall()
    artifacts = {}
    for artifact_type, path in rows:
        artifacts[artifact_type] = path
    return artifacts


def _current_evidence(db_path):
    db_path = Path(db_path)
    if not db_path.exists():
        return {"db_exists": False}
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute(
            """
            SELECT
              t.task_id,
              t.lifecycle,
              rd.route_id,
              a.attempt_id,
              a.status,
              wr.result_id,
              wr.output_type,
              wr.metadata_json
            FROM tasks t
            LEFT JOIN route_decisions rd ON rd.task_id = t.task_id
            LEFT JOIN attempts a ON a.task_id = t.task_id
            LEFT JOIN worker_results wr ON wr.attempt_id = a.attempt_id
            ORDER BY t.created_at DESC, a.attempt_no DESC, wr.created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return {"db_exists": True}
        (
            task_id,
            task_lifecycle,
            route_id,
            attempt_id,
            attempt_status,
            result_id,
            output_type,
            result_metadata_json,
        ) = row
        artifacts = _latest_artifacts(conn, task_id)
        action_rows = conn.execute(
            """
            SELECT audit_status, publish_status, COUNT(*)
            FROM github_actions
            WHERE task_id = ?
            GROUP BY audit_status, publish_status
            """,
            (task_id,),
        ).fetchall()
        accepted_count = sum(count for audit, _publish, count in action_rows if audit == "accepted")
        not_published_count = sum(count for _audit, publish, count in action_rows if publish == "not_published")
    evidence = {
        "db_exists": True,
        "task_id": task_id,
        "task_lifecycle": task_lifecycle,
        "route_id": route_id,
        "attempt_id": attempt_id,
        "attempt_status": attempt_status,
        "result_id": result_id,
        "output_type": output_type,
        "worker_result_metadata": _json_load(result_metadata_json),
        "action_counts": {
            "accepted": accepted_count,
            "not_published": not_published_count,
        },
        "artifacts": artifacts,
    }
    stdout_path = artifacts.get("worker_stdout")
    stderr_path = artifacts.get("worker_stderr")
    if stdout_path:
        evidence["worker_stdout_tail"] = _tail(stdout_path)
    if stderr_path:
        evidence["worker_stderr_tail"] = _tail(stderr_path)
    return evidence


def _wait_for_worker_result(db_path, timeout_seconds, poll_interval_seconds):
    deadline = time.monotonic() + timeout_seconds
    evidence = _current_evidence(db_path)
    while time.monotonic() <= deadline:
        evidence = _current_evidence(db_path)
        if evidence.get("result_id") and evidence.get("attempt_status") == "completed":
            return evidence
        time.sleep(poll_interval_seconds)
    return evidence


def live_worker_acceptance(
    config_path,
    workspace_dir=None,
    fixture_path=None,
    timeout_seconds=600,
    poll_interval_seconds=2,
    workflow_path=DEFAULT_WORKFLOW,
):
    workspace = _workspace_dir(workspace_dir)
    prepared = _isolated_config(config_path, workspace)
    if not prepared["ok"]:
        return prepared

    isolated_config_path = Path(prepared["config_path"])
    db_path = Path(prepared["db_path"])
    if fixture_path:
        dispatch_fixture = Path(fixture_path).expanduser().resolve()
    else:
        dispatch_fixture = _default_fixture(prepared["source_config"], workspace)

    first_run = run_once.run_once(
        isolated_config_path,
        workflow_path=workflow_path,
        fixture_path=dispatch_fixture,
        dry_run=False,
        skip_external=True,
    )
    if not first_run["ok"]:
        evidence = _current_evidence(db_path)
        return {
            "ok": False,
            "status": first_run.get("status", "failed_dispatch"),
            "workspace_dir": str(workspace),
            "config_path": str(isolated_config_path),
            "fixture_path": str(dispatch_fixture),
            "db_path": str(db_path),
            "first_run": first_run,
            "evidence": evidence,
            "safe_error": first_run.get("safe_error", "live worker dispatch failed"),
        }

    worker_evidence = _wait_for_worker_result(db_path, timeout_seconds, poll_interval_seconds)
    if not worker_evidence.get("result_id"):
        return {
            "ok": False,
            "status": "worker_result_timeout",
            "workspace_dir": str(workspace),
            "config_path": str(isolated_config_path),
            "fixture_path": str(dispatch_fixture),
            "db_path": str(db_path),
            "first_run": first_run,
            "evidence": worker_evidence,
            "safe_error": "worker did not record a result before timeout",
        }

    second_run = run_once.run_once(
        isolated_config_path,
        workflow_path=workflow_path,
        dry_run=True,
        skip_external=True,
    )
    final_evidence = _current_evidence(db_path)
    if not second_run["ok"]:
        return {
            "ok": False,
            "status": second_run.get("status", "failed_audit_or_publish"),
            "workspace_dir": str(workspace),
            "config_path": str(isolated_config_path),
            "fixture_path": str(dispatch_fixture),
            "db_path": str(db_path),
            "first_run": first_run,
            "second_run": second_run,
            "evidence": final_evidence,
            "safe_error": second_run.get("safe_error", "dry-run audit or publication failed"),
        }
    if final_evidence["action_counts"]["accepted"] == 0:
        return {
            "ok": False,
            "status": "audit_not_accepted",
            "workspace_dir": str(workspace),
            "config_path": str(isolated_config_path),
            "fixture_path": str(dispatch_fixture),
            "db_path": str(db_path),
            "first_run": first_run,
            "second_run": second_run,
            "evidence": final_evidence,
            "safe_error": "worker result did not produce an accepted GitHub action",
        }

    return {
        "ok": True,
        "status": "completed",
        "workspace_dir": str(workspace),
        "config_path": str(isolated_config_path),
        "fixture_path": str(dispatch_fixture),
        "db_path": str(db_path),
        "route_id": final_evidence.get("route_id"),
        "task_id": final_evidence.get("task_id"),
        "attempt_id": final_evidence.get("attempt_id"),
        "worker_result": {
            "result_id": final_evidence.get("result_id"),
            "output_type": final_evidence.get("output_type"),
            "metadata": final_evidence.get("worker_result_metadata", {}),
        },
        "action_counts": final_evidence["action_counts"],
        "dry_run_publication": second_run.get("publish_result", {}),
        "first_run": first_run,
        "second_run": second_run,
        "artifacts": final_evidence.get("artifacts", {}),
        "worker_stdout_tail": final_evidence.get("worker_stdout_tail", ""),
        "worker_stderr_tail": final_evidence.get("worker_stderr_tail", ""),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace-dir")
    parser.add_argument("--fixture")
    parser.add_argument("--workflow", default=str(DEFAULT_WORKFLOW))
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    result = live_worker_acceptance(
        args.config,
        workspace_dir=args.workspace_dir,
        fixture_path=args.fixture,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        workflow_path=Path(args.workflow),
    )
    if result.get("ok"):
        return emit(result, 0)
    if result.get("status") == "failed_config":
        return emit(result, 3)
    return emit(result, 2)


if __name__ == "__main__":
    raise SystemExit(main())

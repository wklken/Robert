"""Queue prepared attempts and claim launches under the task fence."""

from robert_agent.foundation.contracts.execution import AttemptStatus, RoundStatus

from robert_agent.persistence.queries import attempts as db_read_attempts

from robert_agent.persistence import database as db_database
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.dialects import sqlite as db_sqlite

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import execution_subjects as db_write_execution_subjects
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from robert_agent.supervision import store as supervision_store
from contextlib import closing
from pathlib import Path
from robert_agent.adapters.git import workspace as git_workspace
from robert_agent.adapters.processes import worker as dispatch
from robert_agent.foundation import errors as run_errors
from robert_agent.execution.application import cutover as result_cutover
from robert_agent.execution.application import evidence
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import admission as work_admission
from robert_agent.persistence.queries import dispatch_requests as dispatch_queries
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.configuration import routes as route_config
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import capacity as attempt_capacity
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import ownership as execution_ownership
from robert_agent.execution import retry as retry_policy
from robert_agent.persistence import execution_control as execution_store
from robert_agent.foundation import commands as control_commands
from robert_agent.foundation import values
from robert_agent.execution import rounds as native_rounds
from robert_agent.workflows import routes as route
from robert_agent.workflows import routing as workflow_routing
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store
import json
import subprocess
import sys


def _prepared_attempt_dispatch_priority(conn, attempt_id):
    row = db_records.first(db_read_attempts.find_for_prepared_attempt_dispatch_priority(conn, attempt_id))
    if not row:
        return 2
    route_id, workstream_id = row
    if route_id == "update-existing-pr":
        return 0
    if isinstance(workstream_id, str) and "!" in workstream_id:
        return 1
    return 2


def _record_dispatch_result(conn, attempt_id, dispatch_result):
    if dispatch_result.get("cleanup_pending"):
        row = db_records.first(db_read_attempts.find_task_id_and_metadata_json_by_attempt_id(conn, attempt_id))
        if row:
            metadata = values.json_object(row[1])
            metadata["dispatch"] = dispatch_result
            metadata["supervise"] = {"status": "worker_identity_attention", "reason": "startup_cleanup_unconfirmed"}
            now = values.utc_now()
            db_write_attempts.record_dispatch_result_3(
                conn, now, now, json.dumps(metadata), json.dumps(dispatch_result), attempt_id
            )
            execution_ownership.mark_attention(conn, row[0], now)
        return
    status = dispatch_result.get("status", AttemptStatus.PREPARED.value)
    if status not in {
        AttemptStatus.PREPARED.value,
        AttemptStatus.RUNNING.value,
        AttemptStatus.COMPLETED.value,
        AttemptStatus.FAILED.value,
        AttemptStatus.STALE.value,
        AttemptStatus.CANCELED.value,
    }:
        status = AttemptStatus.FAILED.value if not dispatch_result.get("ok", True) else AttemptStatus.PREPARED.value
    current = db_records.first(db_read_attempts.find_status_and_metadata_json_by_attempt_id(conn, attempt_id))
    if current and current[0] in {
        AttemptStatus.COMPLETED.value,
        AttemptStatus.FAILED.value,
        AttemptStatus.CANCELED.value,
    }:
        metadata = values.json_object(current[1])
        metadata["dispatch"] = dispatch_result
        if dispatch_result.get("status") == "running":
            metadata.setdefault("worker_started_at", values.utc_now())
        db_write_attempts.set_metadata_json_by_attempt_id(
            conn, json.dumps(metadata, ensure_ascii=False, sort_keys=True), attempt_id
        )
        return
    metadata_obj = values.json_object(current[1] if current else None)
    metadata_obj["dispatch"] = dispatch_result
    metadata = json.dumps(metadata_obj, ensure_ascii=False, sort_keys=True)
    if status == AttemptStatus.RUNNING.value:
        started_at = values.utc_now()
        metadata_obj["worker_started_at"] = started_at
        metadata = json.dumps(metadata_obj, ensure_ascii=False, sort_keys=True)
        db_write_attempts.record_dispatch_result(conn, status, started_at, started_at, metadata, attempt_id)
        return
    if status == AttemptStatus.FAILED.value:
        finished_at = values.utc_now()
        failure_json = json.dumps(dispatch_result, ensure_ascii=False, sort_keys=True)
        db_write_attempts.record_dispatch_result_2(conn, status, finished_at, failure_json, metadata, attempt_id)
        return
    db_write_attempts.set_status_and_metadata_json_by_attempt_id(conn, status, metadata, attempt_id)


def _record_dispatch_artifacts(conn, task_info, dispatch_result, run_now):
    for key, artifact_type in [
        ("stdout_path", "worker_stdout"),
        ("stderr_path", "worker_stderr"),
    ]:
        path = dispatch_result.get(key)
        if not path:
            continue
        path_obj = Path(path)
        bytes_size = path_obj.stat().st_size if path_obj.exists() else None
        db_write_artifacts.record_dispatch_artifacts(
            conn,
            values.new_id("artifact"),
            task_info["task_id"],
            task_info["attempt_id"],
            artifact_type,
            str(path_obj),
            bytes_size,
            run_now,
        )


def _completed_by_worker_result(conn, attempt_id):
    row = db_records.first(db_read_attempts.find_for_completed_by_worker_result(conn, attempt_id))
    return bool(row)


def queue_dispatch(
    dispatch_queue,
    task_info,
    worktree_result,
    dispatch_budget=None,
    repo_id=None,
    repo=None,
):
    if not task_info or task_info.get("blocked"):
        return False
    if dispatch_budget is not None:
        repo_remaining = dispatch_budget.get("repo_remaining", {})
        if repo_id in repo_remaining and repo_remaining[repo_id] <= 0:
            return False
        if dispatch_budget["remaining"] <= 0:
            return False
        dispatch_budget["remaining"] -= 1
        if repo_id in repo_remaining:
            repo_remaining[repo_id] -= 1
    dispatch_queue.append(
        {
            "task_info": task_info,
            "worktree_result": worktree_result,
            "repo_id": repo_id,
            "repo": repo,
        }
    )
    return True


def queue_prepared_attempts(conn, repo_id, repo, dispatch_queue, capacity):
    if repo.get("max_concurrency") is not None:
        reserved = sum(item["repo_id"] == repo_id for item in dispatch_queue)
        capacity = min(
            capacity,
            max(0, int(repo["max_concurrency"]) - attempt_capacity.running_attempt_count(conn, repo_id) - reserved),
        )
    reserved_rows = db_read_attempts.find_for_queue_prepared_attempts(conn, repo_id, 0, reserved_only=True)
    reserved_ids = {row[0] for row in reserved_rows}
    rows = reserved_rows + db_read_attempts.find_for_queue_prepared_attempts(conn, repo_id, max(0, capacity))
    queued = 0
    for (
        attempt_id,
        task_id,
        workstream_id,
        route_id,
        worktree_path,
        prompt_path,
        routing_mode,
        requested_worker,
    ) in rows:
        if any(item["task_info"]["attempt_id"] == attempt_id for item in dispatch_queue):
            continue
        if not execution_ownership.claim_task(conn, task_id):
            continue
        task_info = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "workstream_id": workstream_id,
            "route_id": route_id,
            "prompt_path": Path(prompt_path),
            "routing_mode": routing_mode,
            "requested_worker": requested_worker,
        }
        worktree_result = {"worktree_path": worktree_path} if worktree_path else None
        if queue_dispatch(
            dispatch_queue,
            task_info,
            worktree_result,
            repo_id=repo_id,
            repo=repo,
        ):
            queued += int(attempt_id not in reserved_ids)
    return queued


def current_dispatch_target(conn, attempt_id):
    row = db_records.first(db_read_attempts.find_for_current_dispatch_target(conn, attempt_id))
    if not row:
        return None
    (
        task_id,
        workstream_id,
        route_id,
        worktree_path,
        prompt_path,
        routing_mode,
        requested_worker,
        branch_name,
    ) = row
    if not execution_ownership.claim_task(conn, task_id):
        return None
    task_info = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
        "route_id": route_id,
        "prompt_path": Path(prompt_path),
        "routing_mode": routing_mode,
        "requested_worker": requested_worker,
    }
    worktree_result = {"worktree_path": worktree_path, "branch_name": branch_name} if worktree_path else None
    return task_info, worktree_result


def _prepared_attempt_event_and_route(conn, attempt_id):
    row = db_records.first(db_read_attempts.find_for_prepared_attempt_event_and_route(conn, attempt_id))
    if row:
        event = json.loads(row[0])
        route_id = row[1]
        route_result = dict(route.ROUTES.get(route_id) or route.route_task(event))
        return event, route_result
    row = db_records.first(db_read_attempts.find_for_prepared_attempt_event_and_route_2(conn, attempt_id))
    if not row:
        return None, None
    task_metadata = values.json_object(row[5])
    event = {
        "origin_type": "web",
        "number": task_metadata.get("local_source_reference") or row[0].removeprefix("wi-")[:12],
        "title": row[1],
        "body": row[2],
        "existing_pr_head_branch": row[3],
    }
    route_id = row[4]
    route_result = dict(route.ROUTES.get(route_id) or route.route_task(event))
    return event, route_result


def can_materialize_worktree(repo):
    try:
        completed = control_commands.run_command(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo["repo_root"],
            text=True,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return completed.stdout.strip() == "true"


def ensure_dispatch_worktree(db_path, repo, task_info, worktree_result):
    worktree_path = (worktree_result or {}).get("worktree_path")
    if not worktree_path:
        return worktree_result
    if not can_materialize_worktree(repo):
        return worktree_result
    if Path(worktree_path).exists():
        allow_dirty = False
        branch_name = worktree_result.get("branch_name")
        if db_path and task_info.get("attempt_id") and task_info.get("task_id"):
            with closing(db_database.connect(db_path)) as conn:
                ownership = db_records.first(
                    db_read_attempts.find_dispatch_workspace_ownership(
                        conn, task_info["attempt_id"], task_info["task_id"]
                    )
                )
            if not ownership or ownership[0] != worktree_path:
                return {**worktree_result, "ok": False, "status": "blocked_changed_worktree"}
            branch_name = ownership[1]
            allow_dirty = bool(branch_name and ownership[2])
        error = git_workspace.check_worktree_reuse(
            repo["repo_root"], repo["worktree_root"], worktree_path, branch_name=branch_name, allow_dirty=allow_dirty
        )
        return {**worktree_result, "ok": False, "status": error} if error else worktree_result

    with closing(db_database.connect(db_path)) as conn:
        db_sqlite.configure(conn, foreign_keys=True)
        db_sqlite.configure(conn, busy_timeout_ms=5000)
        event, route_result = _prepared_attempt_event_and_route(conn, task_info["attempt_id"])
    if not event or not route_result.get("needs_worktree"):
        return worktree_result

    refreshed = assigned_workspace.prepare_worktree(repo, event, route_result, dry_run=False)
    with closing(db_database.connect(db_path)) as conn, conn:
        db_sqlite.configure(conn, foreign_keys=True)
        db_sqlite.configure(conn, busy_timeout_ms=5000)
        db_write_attempts.set_worktree_path_and_branch_name_by_attempt_id(
            conn, refreshed.get("worktree_path"), refreshed.get("branch_name"), task_info["attempt_id"]
        )
    return refreshed


def _model_for_dispatch(
    conn,
    workstream_id,
    attempt_id,
    selected_worker,
    primary_model,
):
    fallback_model = selected_worker.get("fallback_model")
    current = db_records.first(db_read_attempts.find_metadata_json_by_attempt_id(conn, attempt_id))
    model_fallback = (values.json_object(current[0] if current else None).get("resume") or {}).get("model_fallback")
    if (
        model_fallback
        and model_fallback.get("worker_name") == selected_worker.get("name")
        and model_fallback.get("model") == fallback_model
    ):
        return fallback_model
    if not fallback_model:
        return primary_model
    rows = db_read_attempts.find_for_model_for_dispatch(conn, workstream_id, attempt_id)
    if len(rows) != 2:
        return primary_model
    failures = [values.json_object(row[0]) for row in rows]
    if all(
        failure.get("status") == "failed_timeout" or failure.get("original_status") == "failed_timeout"
        for failure in failures
    ):
        return fallback_model
    return primary_model


def dispatch_queued(
    db_path, repo_ids, dispatch_queue, dry_run, max_concurrency, workers=None, default_worker=None, config_result=None
):
    # The process-owned task lock covers target validation, spawn and PID
    # persistence. Cancellation/publication take this same lock.
    with task_publication_lock(db_path, "dispatch-admission") as admission:
        if not admission:
            return []
        results = []
        if not dry_run and len(dispatch_queue) > 1:
            with closing(db_database.connect(db_path)) as conn:
                dispatch_queue = sorted(
                    dispatch_queue,
                    key=lambda item: _prepared_attempt_dispatch_priority(conn, item["task_info"]["attempt_id"]),
                )
        for item in dispatch_queue:
            with task_publication_lock(db_path, item["task_info"]["task_id"]) as acquired:
                if acquired:
                    results.extend(
                        dispatch_queued_locked(
                            db_path, repo_ids, [item], dry_run, max_concurrency, workers, default_worker, config_result
                        )
                    )
        return results


def dispatch_queued_locked(
    db_path,
    repo_ids,
    dispatch_queue,
    dry_run,
    max_concurrency,
    workers=None,
    default_worker=None,
    config_result=None,
):
    cutover = result_cutover.check(db_path)
    if not cutover["ok"]:
        return [cutover]
    results = []
    default_worker = default_worker or (workers or [])[0]
    workers_by_name = {worker["name"]: worker for worker in (workers or [default_worker])}
    for item in dispatch_queue:
        task_info = item["task_info"]
        worktree_result = item["worktree_result"]
        repo_id = item["repo_id"]
        repo = item["repo"]
        if not dry_run:
            with closing(db_database.connect(db_path)) as conn, conn:
                db_sqlite.configure(conn, foreign_keys=True)
                db_sqlite.configure(conn, busy_timeout_ms=5000)
                slot_credit = int(attempt_capacity.can_resume_native_slot(conn, task_info["attempt_id"]))
                if attempt_capacity.running_attempt_count_for_repos(conn, repo_ids) - slot_credit >= max_concurrency:
                    continue
                if attempt_capacity.running_attempt_count(conn, repo_id) - slot_credit >= int(
                    (repo or {}).get("max_concurrency", max_concurrency)
                ):
                    continue
                current_target = current_dispatch_target(conn, task_info["attempt_id"])
                if current_target is None:
                    continue
                task_info, worktree_result = current_target
            worktree_result = ensure_dispatch_worktree(db_path, repo, task_info, worktree_result)
            with closing(db_database.connect(db_path)) as conn, conn:
                db_sqlite.configure(conn, foreign_keys=True)
                db_sqlite.configure(conn, busy_timeout_ms=5000)
                if not execution_ownership.claim_task(conn, task_info["task_id"]):
                    continue
        effective_route = None
        selected_worker = default_worker
        if task_info.get("routing_mode") == "manual":
            selected_worker = workers_by_name.get(task_info.get("requested_worker"))
            if selected_worker is None:
                with closing(db_database.connect(db_path)) as conn, conn:
                    item = work_items.resolve_work_item_for_workstream(
                        conn,
                        task_info["workstream_id"],
                    )
                    if item:
                        work_items.record_system_event(
                            conn,
                            item["work_item_id"],
                            event_type="manual_worker_unavailable",
                            idempotency_key=(
                                f"manual-worker-unavailable:{task_info['task_id']}:"
                                f"{task_info.get('requested_worker') or 'missing'}"
                            ),
                            body="The selected Agent is unavailable.",
                            metadata={
                                "task_id": task_info["task_id"],
                                "requested_worker": task_info.get("requested_worker"),
                            },
                        )
                    db_write_tasks.update_for_dispatch_queued_locked(conn, task_info["task_id"])
                continue
        elif config_result:
            policy = route_config.ROUTE_POLICIES.get(task_info.get("route_id"))
            if policy:
                effective_route = route_config.resolve_route_config(
                    config_result,
                    repo,
                    policy,
                )
                selected_worker = workers_by_name[effective_route["worker"]]
        primary_model = (
            effective_route["model"]
            if effective_route and task_info.get("routing_mode") != "manual"
            else selected_worker["default_model"]
        )
        with closing(db_database.connect(db_path)) as conn:
            model = _model_for_dispatch(
                conn,
                task_info["workstream_id"],
                task_info["attempt_id"],
                selected_worker,
                primary_model,
            )
        worker_kwargs = {
            "task_id": task_info["task_id"],
            "attempt_id": task_info["attempt_id"],
            "prompt_path": task_info["prompt_path"],
            "worktree_path": (worktree_result or {}).get("worktree_path"),
            "worker_command": selected_worker["command"],
            "worker_agent": selected_worker["agent"],
            "worker_agent_config": selected_worker,
            "model": model,
            "reasoning_effort": (
                effective_route["effort"]
                if effective_route and task_info.get("routing_mode") != "manual"
                else selected_worker["default_effort"]
            ),
            "dry_run": dry_run,
        }
        with closing(db_database.connect(db_path)) as conn:
            native_review = native_rounds.session(conn, task_id=task_info["task_id"])
            dispatch_bound = dispatch_queries.for_task(conn, task_info["task_id"]) is not None
        if not native_review and (config_result or dispatch_bound):
            with closing(db_database.connect(db_path)) as conn, conn:
                if not work_admission.ordinary_dispatch_allowed(conn, task_info["task_id"], repo, config_result):
                    now = values.utc_now()
                    db_write_attempts.update_for_dispatch_queued_locked(conn, now, task_info["attempt_id"])
                    work_application_lifecycle.finalize_failed_task(
                        conn,
                        task_info["task_id"],
                        task_info["workstream_id"],
                        now,
                        {"status": "current_authorization_rejected"},
                    )
                    execution_notifications.insert(
                        conn,
                        "execution_authorization_rejected",
                        "recorded",
                        now,
                        {
                            "task_id": task_info["task_id"],
                            "reason_code": "current_authorization_rejected",
                            "required_action": "Review current trust policy before explicitly retrying.",
                        },
                    )
                    continue
        if (
            native_review
            and config_result
            and not workflow_routing.native_request_allowed(native_review, repo, config_result)
        ):
            with closing(db_database.connect(db_path)) as conn, conn:
                db_transactions.begin(conn, immediate=True)
                current = current_dispatch_target(conn, task_info["attempt_id"])
                if current is not None:
                    now = values.utc_now()
                    db_write_attempts.update_for_dispatch_queued_locked(conn, now, task_info["attempt_id"])
                    db_write_tasks.update_for_dispatch_queued_locked_2(conn, now, task_info["task_id"])
                    db_write_workstreams.update_for_dispatch_queued_locked(conn, now, task_info["task_id"])
                    round_store = vote_store if "source_type" in native_review else review_store
                    round_store.update(
                        conn,
                        native_review["round_id"],
                        status=RoundStatus.CANCELED.value,
                        error="current authorization or native route no longer permits this request",
                    )
                    execution_ownership.close_terminal_task(conn, task_info["task_id"], now)
                    execution_notifications.insert(
                        conn,
                        "native_request_canceled",
                        "recorded",
                        now,
                        {
                            "task_id": task_info["task_id"],
                            "round_id": native_review["round_id"],
                            "reason_code": "current_policy_rejected",
                        },
                    )
            continue
        if config_result and not native_review:
            from robert_agent.workflows.maintenance import repair as supervision_repair

            with closing(db_database.connect(db_path)) as conn, conn:
                if supervision_store.repair_task(conn, task_info["task_id"]):
                    repair_authorization = supervision_repair.authorized_repair(
                        conn,
                        task_info["task_id"],
                        config_result,
                        repo["repo_id"]
                        if "repo_id" in repo
                        else db_records.first(
                            db_read_workstreams.find_repo_id_by_workstream_id(conn, task_info["workstream_id"])
                        )[0],
                    )
                    if not repair_authorization:
                        now = values.utc_now()
                        db_write_attempts.update_for_dispatch_queued_locked_2(conn, now, task_info["attempt_id"])
                        work_application_lifecycle.finalize_failed_task(
                            conn,
                            task_info["task_id"],
                            task_info["workstream_id"],
                            now,
                            {"status": "maintenance_policy_rejected"},
                        )
                        continue
                    from robert_agent.workflows.maintenance import worker as supervision_repair_worker

                    worker_kwargs["worker_agent_config"] = supervision_repair_worker.launch_profile(
                        config_result, task_info["task_id"], task_info["attempt_id"], repair_authorization["worker"]
                    )
        if native_review:
            configured = native_review["request"]["workers"]
            timeout = (config_result or {}).get("hard_timeout_seconds", 3600)
            environment_names = sorted(
                {"GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "CODEX_HOME", "CLAUDE_CONFIG_DIR"}
                | {name for w in configured for name in w["environment_allowlist"]}
            )
            worker_kwargs.update(
                worktree_path=str(Path(db_path).resolve().parent),
                worker_agent_config={
                    "agent": "command",
                    "command_argv": [
                        sys.executable,
                        "-m",
                        (
                            "robert_agent.workflows.vote.coordinator"
                            if "source_type" in native_review
                            else "robert_agent.workflows.review.coordinator"
                        ),
                        "--db",
                        str(Path(db_path).resolve()),
                        "--task-id",
                        task_info["task_id"],
                        "--attempt-id",
                        task_info["attempt_id"],
                    ],
                    "timeout_seconds": timeout,
                    "environment_allowlist": environment_names,
                },
            )
        if not dry_run:
            with closing(db_database.connect(db_path)) as conn, conn:
                db_transactions.begin(conn, immediate=True)
                target = current_dispatch_target(conn, task_info["attempt_id"])
                if target is None:
                    continue
                task_info, current_workspace = target
                if any(
                    (current_workspace or {}).get(key) != (worktree_result or {}).get(key)
                    for key in ("worktree_path", "branch_name")
                ):
                    worktree_result = {"ok": False, "status": "workspace_changed_before_dispatch"}
                if not execution_ownership.claim_task(conn, task_info["task_id"]):
                    continue
                subject_type, subject_id = execution_ownership.subject_for_task(conn, task_info["task_id"])
                binding = execution_store.get_binding(conn, subject_type, subject_id)
                metadata = values.json_object(binding["metadata_json"]) if binding else {}
                if metadata.get("dispatch_inflight"):
                    if binding:
                        metadata["reason_code"] = "dispatch_outcome_unknown"
                        db_write_execution_subjects.update_for_dispatch_queued_locked_2(
                            conn, json.dumps(metadata), subject_type, subject_id
                        )
                    continue
                if binding:
                    metadata["dispatch_inflight"] = task_info["attempt_id"]
                    db_write_execution_subjects.update_for_dispatch_queued_locked(
                        conn, json.dumps(metadata), subject_type, subject_id
                    )
        with dispatch.defer_termination():
            try:
                if worktree_result and not worktree_result.get("ok", True):
                    dispatch_result = {
                        "ok": False,
                        "status": "failed_dispatch",
                        "safe_error": worktree_result.get("status", "worktree_unavailable"),
                    }
                else:
                    dispatch_result = dispatch.dispatch_worker(**worker_kwargs)
            except Exception as exc:
                dispatch_result = {
                    **run_errors.safe_dispatch_error(exc),
                    "task_id": task_info["task_id"],
                    "attempt_id": task_info["attempt_id"],
                }
            dispatch_logs = (
                {
                    kind: {"tail": evidence.tail_file(dispatch_result.get(f"{kind}_path"))}
                    for kind in ("stdout", "stderr")
                }
                if not dispatch_result.get("ok", True) and dispatch_result.get("pid")
                else {}
            )
            if (
                not dispatch_result.get("ok", True)
                and dispatch_result.get("pid")
                and (
                    evidence.terminal_transport_failure(dispatch_logs)
                    or retry_policy.terminal_rate_limit(dispatch_logs)
                )
            ):
                # Even an exit during the startup probe must pass through the same
                # supervised, bounded continuation path as a later stream failure.
                dispatch_result = {
                    **dispatch_result,
                    "ok": True,
                    "status": "running",
                    "original_status": dispatch_result.get("status"),
                    "process_exited": True,
                }
            dispatch_result = {
                **dispatch_result,
                "worker_name": selected_worker["name"],
                "model": model,
                "fallback_model": selected_worker.get("fallback_model"),
                "reasoning_effort": worker_kwargs["reasoning_effort"],
            }
            with closing(db_database.connect(db_path)) as conn, conn:
                db_sqlite.configure(conn, foreign_keys=True)
                db_sqlite.configure(conn, busy_timeout_ms=5000)
                if not execution_ownership.claim_task(conn, task_info["task_id"]):
                    # Process evidence survives lost authority; cleanup must not hold a writer.
                    _record_dispatch_result(conn, task_info["attempt_id"], dispatch_result)
                    conn.commit()
                    stopped = dispatch.stop_dispatch(dispatch_result)
                    dispatch_result = {
                        **dispatch_result,
                        "ok": False,
                        "status": "failed_dispatch",
                        "safe_error": "Task ownership changed during worker launch.",
                        "cleanup_pending": not stopped,
                    }
                    _record_dispatch_result(conn, task_info["attempt_id"], dispatch_result)
                    if stopped:
                        current = db_records.first(
                            db_read_attempts.find_status_and_metadata_json_by_attempt_id(conn, task_info["attempt_id"])
                        )
                        if current:
                            db_write_attempts.update_for_prepare_2(
                                conn, values.utc_now(), task_info["attempt_id"], current[0], current[1]
                            )
                    if not stopped:
                        execution_notifications.insert(
                            conn,
                            "worker_identity_attention",
                            "recorded",
                            values.utc_now(),
                            {
                                "task_id": task_info["task_id"],
                                "attempt_id": task_info["attempt_id"],
                                "reason_code": "startup_cleanup_unconfirmed",
                                "required_action": "Verify the launch process group has stopped, then cancel this execution.",
                            },
                        )
                    results.append(dispatch_result)
                    continue
                _record_dispatch_result(conn, task_info["attempt_id"], dispatch_result)
                subject_type, subject_id = execution_ownership.subject_for_task(conn, task_info["task_id"])
                binding = execution_store.get_binding(conn, subject_type, subject_id)
                if binding:
                    metadata = values.json_object(binding["metadata_json"])
                    metadata.pop("dispatch_inflight", None)
                    db_write_execution_subjects.update_for_dispatch_queued_locked(
                        conn, json.dumps(metadata), subject_type, subject_id
                    )
                _record_dispatch_artifacts(conn, task_info, dispatch_result, values.utc_now())
                if dispatch_result.get("cleanup_pending"):
                    execution_notifications.insert(
                        conn,
                        "worker_identity_attention",
                        "recorded",
                        values.utc_now(),
                        {
                            "task_id": task_info["task_id"],
                            "attempt_id": task_info["attempt_id"],
                            "workstream_id": task_info["workstream_id"],
                            "reason_code": "startup_cleanup_unconfirmed",
                            "required_action": "Verify the launch process group has stopped, then cancel this execution.",
                        },
                    )
                    results.append(dispatch_result)
                    continue
                if (
                    not dispatch_result.get("ok", True) or dispatch_result.get("status") == "failed"
                ) and _completed_by_worker_result(conn, task_info["attempt_id"]):
                    dispatch_result = {
                        **dispatch_result,
                        "ok": True,
                        "status": "completed_by_worker_result",
                        "original_status": dispatch_result.get("status"),
                    }
                if not dispatch_result.get("ok", True) or dispatch_result.get("status") == "failed":
                    run_now = values.utc_now()
                    work_application_lifecycle.finalize_failed_task(
                        conn,
                        task_info["task_id"],
                        task_info["workstream_id"],
                        run_now,
                        dispatch_result,
                    )
                    execution_notifications.insert(
                        conn,
                        "worker_dispatch_failed",
                        "recorded",
                        run_now,
                        {
                            "task_id": task_info["task_id"],
                            "attempt_id": task_info["attempt_id"],
                            "workstream_id": task_info["workstream_id"],
                            "safe_error": dispatch_result.get("safe_error", ""),
                        },
                    )
            results.append(dispatch_result)
    return results

"""Prepare an attempt with durable inputs, a workspace and a prompt."""

from robert_agent.foundation.contracts.delivery import OutputType
from robert_agent.foundation.contracts.execution import AttemptStatus, RoundStatus
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, TaskKind, TaskLifecycle, WorkstreamLifecycle

from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import repos as db_read_repos
from robert_agent.persistence.queries import route_decisions as db_read_route_decisions
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import workstreams as db_read_workstreams
from robert_agent.persistence.stores import artifacts as db_write_artifacts
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import route_decisions as db_write_route_decisions
from robert_agent.persistence.stores import task_events as db_write_task_events
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import work_items as db_write_work_items
from robert_agent.persistence.stores import workstream_sources as db_write_workstream_sources
from robert_agent.persistence.stores import workstreams as db_write_workstreams
from datetime import timedelta
from pathlib import Path
from robert_agent.execution import retry as retry_policy
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.work.application import participants
from robert_agent.work.application import source_events
from robert_agent.context import attempt as attempt_context
from robert_agent.context import prompts as render_prompt
from robert_agent.delivery import contract as result_contract
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import ownership as execution_ownership
from robert_agent.foundation import values
from robert_agent.supervision import lineage as supervision_lineage
from robert_agent.work import queries as work_queries
from robert_agent.work import streams as workstream
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.workflows import routes as route
from robert_agent.workflows import routing as workflow_routing
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store
import json


def insert_file_artifact(conn, task_id, attempt_id, artifact_type, path, run_now):
    path_obj = Path(path)
    bytes_size = path_obj.stat().st_size if path_obj.exists() else None
    db_write_artifacts.record_dispatch_artifacts(
        conn, values.new_id("artifact"), task_id, attempt_id, artifact_type, str(path_obj), bytes_size, run_now
    )


def upsert_route_decision(conn, task_id, route_result, run_now):
    required_skills = route_result.get("required_skills", [])
    recommended_skills = route_result.get("recommended_skills", [])
    row = db_records.first(db_read_route_decisions.find_route_decision_id_by_task_id(conn, task_id))
    if row:
        db_write_route_decisions.update_for_upsert_route_decision(
            conn,
            route_result["route_id"],
            route_result["expected_output"],
            json.dumps(route_result["allowed_github_actions"], sort_keys=True),
            json.dumps(required_skills, sort_keys=True),
            json.dumps(recommended_skills, sort_keys=True),
            route_result["confidence"],
            run_now,
            row[0],
        )
        return
    db_write_route_decisions.insert_for_upsert_route_decision(
        conn,
        values.new_id("route"),
        task_id,
        route_result["route_id"],
        route_result["expected_output"],
        json.dumps(route_result["allowed_github_actions"], sort_keys=True),
        json.dumps(required_skills, sort_keys=True),
        json.dumps(recommended_skills, sort_keys=True),
        route_result["confidence"],
        run_now,
    )


def refresh_prepared_task(conn, data_dir, db_path, repo, event, run_now, dry_run, config_result=None):
    state = source_events.active_task_state(conn, event["workstream_id"])
    if not state:
        return None
    # Persist event ingestion before contending with dispatch. No SQLite writer
    # lock may be retained while acquiring the process lock or preparing Git.
    conn.commit()
    with task_publication_lock(db_path, state["task_id"]) as acquired:
        if not acquired:
            return None
        current = source_events.active_task_state(conn, event["workstream_id"])
        owner = db_records.first(db_read_workstreams.find_active_task_id_by_workstream_id(conn, event["workstream_id"]))
        if (
            current != state
            or not owner
            or owner[0] != state["task_id"]
            or current["task_lifecycle"] != TaskLifecycle.QUEUED
            or current["attempt_status"] != AttemptStatus.PREPARED
        ):
            return None
        prompt_path = attempt_context.prompt_path_for_task(conn, data_dir, state["task_id"])
        paths = [
            prompt_path,
            *(
                Path(path)
                for path in attempt_context.github_context_paths(prompt_path.parent, state["attempt_id"]).values()
            ),
        ]
        previous = {path: path.read_bytes() if path.exists() else None for path in paths}
        try:
            result = _refresh_prepared_task_locked(
                conn, data_dir, db_path, repo, event, run_now, dry_run, config_result
            )
            conn.commit()  # Publish metadata before dispatch can acquire this lock.
            return result
        except BaseException:
            conn.rollback()
            attempt_context.atomic_replace_files(
                {path: content for path, content in previous.items() if content is not None}
            )
            for path, content in previous.items():
                if content is None:
                    path.unlink(missing_ok=True)
            raise


def _refresh_prepared_task_locked(
    conn,
    data_dir,
    db_path,
    repo,
    event,
    run_now,
    dry_run,
    config_result=None,
):
    state = source_events.active_task_state(conn, event["workstream_id"])
    if not state:
        return None
    if not execution_ownership.claim_task(conn, state["task_id"], run_now):
        return None
    if state["task_lifecycle"] != TaskLifecycle.QUEUED or state["attempt_status"] != AttemptStatus.PREPARED:
        return None
    if event.get("source_type") != "pull_request" or not event.get("has_open_dd_pr"):
        return None
    if state["route_id"] not in {"new-pr", "update-existing-pr"}:
        return None

    route_result = workflow_routing.effective_route(config_result, repo, event)
    if route_result["route_id"] != "update-existing-pr":
        return None
    input_policy = route.review_input_policy(route_result["expected_output"], event)
    if (
        state["expected_output"] in {OutputType.UPDATE_EXISTING_PR, OutputType.REVIEW_COMMENT}
        and source_events.task_review_input_policy(conn, state["task_id"]) == "required"
    ):
        input_policy = "required"
    prompt_path = attempt_context.prompt_path_for_task(conn, data_dir, state["task_id"])
    context_path = Path(attempt_context.github_context_paths(prompt_path.parent, state["attempt_id"])["json_path"])
    try:
        prepared_events = json.loads(context_path.read_text(encoding="utf-8"))["events"]
        event_known = any(item.get("event_fingerprint") == event["event_fingerprint"] for item in prepared_events)
    except (OSError, ValueError, KeyError, TypeError):
        event_known = False
    if (
        event_known
        and state["route_id"] == route_result["route_id"]
        and state["expected_output"] == route_result["expected_output"]
        and source_events.task_review_input_policy(conn, state["task_id"]) == input_policy
    ):
        return None

    conn.commit()  # External Git I/O must not retain the SQLite writer lock.
    worktree_result = assigned_workspace.prepare_worktree(repo, event, route_result, dry_run=dry_run)
    # Recheck after external preparation as well. Execution control can revoke
    # ownership while Git is running; no stale input may be installed afterward.
    current = source_events.active_task_state(conn, event["workstream_id"])
    owner = db_records.first(db_read_workstreams.find_active_task_id_by_workstream_id(conn, event["workstream_id"]))
    if (
        current != state
        or not owner
        or owner[0] != state["task_id"]
        or not execution_ownership.claim_task(conn, state["task_id"], run_now)
    ):
        return None
    conn.commit()
    related_events = work_queries.task_related_events(conn, state["task_id"])
    repo_id = values.repository_id(repo["full_name"])
    runtime_knowledge_items = attempt_context.load_runtime_knowledge(
        conn,
        repo_id,
        event["workstream_id"],
        route_result,
        related_events,
    )
    prompt_path = attempt_context.prompt_path_for_task(conn, data_dir, state["task_id"])
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    github_context = attempt_context.write_github_context_artifacts(
        prompt_path.parent, state["attempt_id"], related_events
    )
    github_context["current_event_fingerprint"] = event["event_fingerprint"]
    runtime_context = attempt_context.runtime_context(db_path, repo, worktree_result)
    runtime_context["github_context"] = github_context
    runtime_context["publication_targets"] = result_contract.target_urls(
        {
            "repo": repo["full_name"],
            "sources": work_queries.sources_for_workstream(conn, event["workstream_id"]),
        }
    )
    prompt_text = render_prompt.render_prompt(
        {
            "task_id": state["task_id"],
            "attempt_id": state["attempt_id"],
            "workstream_id": event["workstream_id"],
            "review_input_policy": input_policy,
        },
        route_result,
        related_events,
        runtime_context=runtime_context,
        runtime_knowledge=runtime_knowledge_items,
        project_memories=attempt_context.retrieve_project_memories(
            conn,
            repo_id,
            event["workstream_id"],
            route_result,
            related_events,
            runtime_knowledge_items,
        ),
    )
    attempt_context.atomic_replace_files({prompt_path: prompt_text.encode("utf-8")})
    for artifact_type, path in [
        ("prompt", str(prompt_path)),
        ("github_context_json", github_context["json_path"]),
        ("github_context_md", github_context["md_path"]),
    ]:
        updated = db_write_artifacts.update_for_refresh_prepared_task_locked(
            conn, Path(path).stat().st_size, run_now, state["attempt_id"], artifact_type, path
        )
        if not updated:
            insert_file_artifact(conn, state["task_id"], state["attempt_id"], artifact_type, path, run_now)
    db_write_tasks.update_for_refresh_prepared_task_locked(
        conn, route_result["route_id"], route_result["expected_output"], run_now, state["task_id"]
    )
    db_write_tasks.update_for_refresh_prepared_task_locked_2(
        conn, input_policy, state["task_id"], event["event_fingerprint"]
    )
    upsert_route_decision(conn, state["task_id"], route_result, run_now)
    attempt_metadata_row = db_records.first(
        db_read_attempts.find_metadata_json_by_attempt_id(conn, state["attempt_id"])
    )
    attempt_metadata = values.json_object(attempt_metadata_row[0] if attempt_metadata_row else None)
    existing_pr_head_sha = source_events.event_existing_pr_head_sha(event)
    if existing_pr_head_sha:
        attempt_metadata["existing_pr_head_sha"] = existing_pr_head_sha
    db_write_attempts.update_for_refresh_prepared_task_locked(
        conn,
        (worktree_result or {}).get("worktree_path"),
        (worktree_result or {}).get("branch_name"),
        json.dumps(attempt_metadata, ensure_ascii=False, sort_keys=True),
        state["attempt_id"],
    )
    skill_status = route_result.get("skill_status") or {"runnable": True}
    workspace_blocked = bool(worktree_result and not worktree_result.get("ok", True))
    if not skill_status["runnable"] or workspace_blocked:
        reason = (
            worktree_result.get("status", "worktree_unavailable")
            if workspace_blocked
            else "required_route_skills_missing"
        )
        db_write_attempts.update_for_refresh_prepared_task_locked_2(conn, run_now, state["attempt_id"])
        work_application_lifecycle.finalize_failed_task(
            conn, state["task_id"], event["workstream_id"], run_now, {"status": reason}
        )
        execution_notifications.insert(
            conn, reason, "recorded", run_now, metadata={"task_id": state["task_id"], "safe_error": reason}
        )
    return prompt_path


def create_task_attempt_and_prompt(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    event,
    route_result,
    stream,
    run_now,
    worktree_result=None,
    parent_task_id=None,
    related_events=None,
    retrigger_wakeup_id=None,
    existing_task_id=None,
    new_request_event=None,
    native_round_id=None,
):
    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    remediation_context = (event.get("metadata") or {}).get("remediation") or {}
    selected_round = None
    if native_round_id:
        round_store = vote_store if route_result.get("native_vote_workers") else review_store
        selected_round = round_store.session(conn, round_id=native_round_id)
        if (
            not selected_round
            or selected_round["task_id"]
            or selected_round["status"] != RoundStatus.QUEUED
            or selected_round["workstream_id"] != stream["workstream_id"]
            or selected_round["request"]["event"]["event_fingerprint"] != event["event_fingerprint"]
        ):
            return None
    existing = None
    if existing_task_id:
        existing = db_records.first(
            db_read_tasks.find_for_create_task_attempt_and_prompt(conn, existing_task_id, stream["workstream_id"])
        )
        if not existing or not execution_ownership.claim_task(conn, existing_task_id, run_now):
            return None
    if existing_task_id:
        lineage_key = supervision_lineage.get_task_lineage(conn, existing_task_id)
    elif selected_round:
        lineage_key = selected_round["request"]["event"].get(supervision_lineage.LINEAGE_FIELD)
    elif new_request_event is not None:
        lineage_key = supervision_lineage.github_key(new_request_event)
    elif parent_task_id:
        # A continuation of a legacy request stays unassigned; a parent link
        # alone is not evidence that an old request has a valid recovery chain.
        lineage_key = supervision_lineage.get_task_lineage(conn, parent_task_id)
    elif remediation_context.get("episode_id"):
        lineage_key = supervision_lineage.episode_key(remediation_context["episode_id"])
    else:
        lineage_key = supervision_lineage.github_key(event)
    if new_request_event is not None:
        # History linkage does not transfer ownership of the old execution.
        # Recheck after worktree I/O, which releases the SQLite writer lock.
        if not source_events.github_followup_allows_new_subject(conn, repo, new_request_event, parent_task_id):
            return None
    elif (
        not existing_task_id
        and parent_task_id
        and not execution_ownership.parent_allows_child(conn, parent_task_id, retrigger_wakeup_id=retrigger_wakeup_id)
    ):
        return None
    if not (route_result.get("native_review_workers") or route_result.get("native_vote_workers")):
        # Native rounds have a separate explicit-request admission contract;
        # canceling one round does not cancel later independent review requests.
        item = work_items.resolve_work_item_for_workstream(conn, stream["workstream_id"])
        if (
            item
            and item["canceled_at"]
            and (new_request_event is None or source_events.operator_canceled_item(conn, item))
        ):
            return None
        current_stream = db_records.first(
            db_read_workstreams.find_lifecycle_by_workstream_id(conn, stream["workstream_id"])
        )
        if current_stream and current_stream[0] == WorkstreamLifecycle.CANCELED.value and new_request_event is None:
            return None
    if remediation_context.get("episode_id") and not execution_ownership.claim_episode(
        conn,
        remediation_context["episode_id"],
        run_now,
    ):
        return None
    skill_status = route_result.get("skill_status") or {
        "runnable": True,
        "missing_required": [],
        "missing_recommended": [],
    }
    workspace_blocked = bool(worktree_result and not worktree_result.get("ok", True))
    blocked = not skill_status["runnable"] or workspace_blocked
    lifecycle = WorkstreamLifecycle.FAILED.value if blocked else WorkstreamLifecycle.ACTIVE.value
    task_lifecycle = TaskLifecycle.FAILED.value if blocked else TaskLifecycle.QUEUED.value
    attempt_status = AttemptStatus.FAILED.value if blocked else AttemptStatus.PREPARED.value
    related_events = list(related_events or [event])
    if route_result.get("native_review_workers") or route_result.get("native_vote_workers"):
        related_events = [event]
    refreshed_context = None
    if retrigger_wakeup_id:
        saved = db_records.first(db_read_wakeups.find_metadata_json_by_wakeup_id(conn, retrigger_wakeup_id))
        refreshed_context = json.loads(saved[0]).get("refreshed_context") if saved else None
    elif parent_task_id and new_request_event is None:
        saved = db_records.first(db_read_tasks.find_metadata_json_by_task_id(conn, parent_task_id))
        refreshed_context = json.loads(saved[0]).get("refreshed_context") if saved else None
    workstream_id = stream["workstream_id"]
    source_id = values.github_source_id(event["source_key"])
    task_id = existing_task_id or values.new_id("task")
    attempt_id = values.new_id("attempt")
    prompt_dir = data_dir / "artifacts" / task_id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    task = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
        "task_kind": event.get("task_kind", TaskKind.HUMAN_REQUEST.value),
        "review_input_policy": route.review_input_policy(route_result["expected_output"], event),
    }
    runtime_knowledge_items = attempt_context.load_runtime_knowledge(
        conn,
        repo_id,
        workstream_id,
        route_result,
        related_events,
    )
    project_memories = attempt_context.retrieve_project_memories(
        conn,
        repo_id,
        workstream_id,
        route_result,
        related_events,
        runtime_knowledge_items,
    )
    github_context = attempt_context.write_github_context_artifacts(prompt_dir, attempt_id, related_events)
    runtime_context = attempt_context.runtime_context(db_path, repo, worktree_result)
    runtime_context["github_context"] = github_context

    db_write_workstreams.insert_for_create_task_attempt_and_prompt(
        conn, workstream_id, repo_id, source_id, lifecycle, task_id, run_now, run_now
    )
    item = work_items.ensure_github_work_item(
        conn,
        repo_id=repo_id,
        source_id=source_id,
        workstream_id=workstream_id,
        actor_identity=event.get("actor_login") or "github",
        route_confidence=route_result.get("confidence", "low"),
        now=run_now,
    )
    if item["origin_type"] == "github" and item["canceled_at"] and new_request_event is not None:
        db_write_work_items.update_for_create_task_attempt_and_prompt(conn, run_now, item["work_item_id"])
    if item["origin_type"] == "github" and item["completed_at"] and not item["canceled_at"]:
        db_write_work_items.update_for_create_task_attempt_and_prompt_2(conn, run_now, item["work_item_id"])
    participants.record_review_participant(conn, workstream_id, event, route_result, run_now)
    for related_event in related_events:
        related_source_id = values.github_source_id(related_event["source_key"])
        db_write_workstream_sources.insert_for_create_task_attempt_and_prompt(
            conn, workstream_id, related_source_id, workstream.source_relationship(related_event), run_now
        )
    runtime_context["publication_targets"] = result_contract.target_urls(
        {
            "repo": repo["full_name"],
            "sources": work_queries.sources_for_workstream(conn, workstream_id),
        }
    )
    prompt_path.write_text(
        render_prompt.render_prompt(
            task,
            route_result,
            related_events,
            runtime_context=runtime_context,
            runtime_knowledge=runtime_knowledge_items,
            project_memories=project_memories,
        ),
        encoding="utf-8",
    )
    task_metadata = {
        **(values.json_object(existing[0]) if existing else {}),
        **({supervision_lineage.LINEAGE_FIELD: lineage_key} if lineage_key else {}),
        "review_input_policy": task["review_input_policy"],
        "request_event_fingerprint": event["event_fingerprint"],
        **(
            {"refreshed_context": related_events}
            if existing
            else {"refreshed_context": refreshed_context}
            if refreshed_context
            else {}
        ),
        **(
            {"remediation_episode_id": remediation_context.get("episode_id")}
            if event.get("task_kind") in REMEDIATION_TASK_KINDS
            else {}
        ),
        **(
            {"new_request_event_id": values.github_event_id(new_request_event["event_fingerprint"])}
            if new_request_event is not None
            else {}
        ),
    }
    if existing:
        db_write_tasks.update_for_create_task_attempt_and_prompt(
            conn,
            task_lifecycle,
            route_result["route_id"],
            route_result["expected_output"],
            run_now,
            json.dumps(task_metadata, ensure_ascii=False),
            task_id,
        )
    else:
        db_write_tasks.insert_for_create_task_attempt_and_prompt(
            conn,
            task_id,
            workstream_id,
            event.get("task_kind", TaskKind.HUMAN_REQUEST.value),
            TaskLifecycle.QUEUED.value
            if blocked and event.get("task_kind", TaskKind.HUMAN_REQUEST) == TaskKind.HUMAN_REQUEST
            else task_lifecycle,
            parent_task_id,
            route_result["route_id"],
            route_result["expected_output"],
            run_now,
            run_now,
            json.dumps(task_metadata, ensure_ascii=False, sort_keys=True),
        )
    if not execution_ownership.claim_task(conn, task_id, run_now):
        if not existing:
            db_write_tasks.delete_for_create_task_attempt_and_prompt(conn, task_id)
        return None
    if route_result.get("native_review_workers") or route_result.get("native_vote_workers"):
        round_store = vote_store if route_result.get("native_vote_workers") else review_store
        workers = route_result.get("native_vote_workers") or route_result["native_review_workers"]
        recovery_key = None
        if retrigger_wakeup_id and parent_task_id:
            parent = db_records.first(db_read_tasks.find_metadata_json_by_task_id(conn, parent_task_id))
            previous_round = round_store.session(conn, task_id=parent_task_id)
            if (previous_round and previous_round["status"] == RoundStatus.SUPERSEDED.value) or (
                parent
                and values.json_object(parent[0]).get("recovered_execution_attention", {}).get("reason_code")
                == "result_contract_cutover"
            ):
                recovery_key = retrigger_wakeup_id
        entry = (
            round_store.session(conn, round_id=native_round_id)
            if native_round_id
            else round_store.enqueue(
                conn,
                repo,
                {**event, supervision_lineage.LINEAGE_FIELD: lineage_key},
                route_result,
                workers,
                recovery_key=recovery_key,
            )
        )
        round_store.update(conn, entry["round_id"], task_id=task_id, status=RoundStatus.QUEUED.value, error=None)
    if blocked and event.get("task_kind", TaskKind.HUMAN_REQUEST) == TaskKind.HUMAN_REQUEST:
        db_write_tasks.update_for_supervise_running_attempts(conn, run_now, task_id)
        execution_ownership.close_terminal_task(conn, task_id, run_now)
    for index, related_event in enumerate(related_events):
        relationship = "trigger" if index == 0 else "context"
        db_write_task_events.insert_for_create_task_attempt_and_prompt(
            conn, task_id, values.github_event_id(related_event["event_fingerprint"]), relationship, run_now
        )
    db_write_route_decisions.insert_for_upsert_route_decision(
        conn,
        values.new_id("route"),
        task_id,
        route_result["route_id"],
        route_result["expected_output"],
        json.dumps(route_result["allowed_github_actions"], sort_keys=True),
        json.dumps(route_result.get("required_skills", []), sort_keys=True),
        json.dumps(route_result.get("recommended_skills", []), sort_keys=True),
        route_result["confidence"],
        run_now,
    )
    existing_pr_head_sha = source_events.event_existing_pr_head_sha(event)
    attempt_metadata = {"existing_pr_head_sha": existing_pr_head_sha} if existing_pr_head_sha else {}
    attempt_metadata["result_contract"] = result_contract.CONTRACT_ID
    db_write_attempts.insert_for_create_task_attempt_and_prompt(
        conn,
        attempt_id,
        task_id,
        attempt_status,
        (worktree_result or {}).get("worktree_path"),
        (worktree_result or {}).get("branch_name"),
        run_now,
        run_now,
        json.dumps(attempt_metadata, ensure_ascii=False, sort_keys=True),
    )
    for artifact_type, path in [
        ("prompt", prompt_path),
        ("github_context_json", github_context["json_path"]),
        ("github_context_md", github_context["md_path"]),
    ]:
        insert_file_artifact(conn, task_id, attempt_id, artifact_type, path, run_now)
    if blocked:
        execution_notifications.insert(
            conn,
            "worktree_unavailable" if workspace_blocked else "required_route_skills_missing",
            "recorded",
            run_now,
            metadata={
                "task_id": task_id,
                "safe_error": (
                    worktree_result.get("status", "worktree_unavailable")
                    if workspace_blocked
                    else "missing required route skills: " + ", ".join(skill_status["missing_required"])
                ),
            },
        )
    if blocked and remediation_context.get("episode_id"):
        remediation.exhaust_episode(
            conn,
            episode_id=remediation_context["episode_id"],
            reason="required_route_skills_missing",
            now=run_now,
        )
    if not blocked and remediation_context.get("episode_id"):
        remediation.record_task_started(
            conn,
            episode_id=remediation_context["episode_id"],
            task_id=task_id,
            now=run_now,
        )
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
        "prompt_path": prompt_path,
        "route_id": route_result["route_id"],
        "blocked": blocked,
    }


def _latest_route_result_for_task(conn, task_id):
    row = db_records.first(db_read_route_decisions.find_for_latest_route_result_for_task(conn, task_id))
    if not row:
        return None
    (
        route_id,
        expected_output,
        allowed_actions_json,
        required_skills_json,
        recommended_skills_json,
        confidence,
    ) = row
    return {
        "route_id": route_id,
        "expected_output": expected_output,
        "allowed_github_actions": values.json_list(allowed_actions_json),
        "required_skills": values.json_list(required_skills_json),
        "recommended_skills": values.json_list(recommended_skills_json),
        "verification_policy": route.verification_policy_for(route_id),
        "confidence": confidence,
        "needs_worktree": bool((route.ROUTES.get(route_id) or {}).get("needs_worktree")),
    }


def _events_for_task(conn, task_id):
    rows = db_read_task_events.find_for_events_for_task(conn, task_id)
    return [json.loads(payload) for _relationship, payload in rows]


def create_resume_attempt_and_prompt(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    task_id,
    workstream_id,
    previous_attempt_id,
    run_now,
    recovery_context,
):
    previous = db_records.first(
        db_read_attempts.find_attempt_no_and_worktree_path_and_branch_name_and_metadata_json_by_attempt_id(
            conn, previous_attempt_id
        )
    )
    if not previous:
        return None
    max_attempt_no = db_records.first(db_read_attempts.find_for_create_resume_attempt_and_prompt(conn, task_id))[0]
    attempt_id = values.new_id("attempt")
    attempt_no = max_attempt_no + 1
    _previous_attempt_no, worktree_path, branch_name, previous_metadata_json = previous
    route_result = _latest_route_result_for_task(conn, task_id)
    events = _events_for_task(conn, task_id)
    if not route_result or not events:
        return None

    prompt_dir = data_dir / "artifacts" / task_id
    prompt_dir.mkdir(parents=True, exist_ok=True)
    recovery_path = prompt_dir / f"{attempt_id}.recovery.json"
    recovery_context = {
        **recovery_context,
        "strategy": "resume_from_worktree",
        "new_attempt_id": attempt_id,
        "recovery_artifact_path": str(recovery_path),
    }
    recovery_path.write_text(
        json.dumps(recovery_context, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    task = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "workstream_id": workstream_id,
        "review_input_policy": source_events.task_review_input_policy(conn, task_id),
    }
    worktree_result = {
        "worktree_path": worktree_path or "",
        "branch_name": branch_name or "",
    }
    runtime_knowledge_items = attempt_context.load_runtime_knowledge(
        conn,
        repo_id,
        workstream_id,
        route_result,
        events,
    )
    project_memories = attempt_context.retrieve_project_memories(
        conn,
        repo_id,
        workstream_id,
        route_result,
        events,
        runtime_knowledge_items,
    )
    runtime_context = attempt_context.runtime_context(db_path, repo, worktree_result)
    runtime_context["recovery_context"] = recovery_context
    prompt_path = prompt_dir / f"{attempt_id}.prompt.md"
    github_context = attempt_context.write_github_context_artifacts(prompt_dir, attempt_id, events)
    runtime_context["github_context"] = github_context
    repo_full_name = db_records.first(db_read_repos.find_full_name_by_repo_id(conn, repo_id))[0]
    runtime_context["publication_targets"] = result_contract.target_urls(
        {
            "repo": repo_full_name,
            "sources": work_queries.sources_for_workstream(conn, workstream_id),
        }
    )
    prompt_path.write_text(
        render_prompt.render_prompt(
            task,
            route_result,
            events,
            runtime_context=runtime_context,
            runtime_knowledge=runtime_knowledge_items,
            project_memories=project_memories,
        ),
        encoding="utf-8",
    )

    attempt_metadata = {
        "resume": {
            "previous_attempt_id": previous_attempt_id,
            "strategy": "resume_from_worktree",
            "recovery_artifact_path": str(recovery_path),
        }
    }
    previous_resume = values.json_object(previous_metadata_json).get("resume") or {}
    model_fallback = recovery_context.get("model_fallback") or previous_resume.get("model_fallback")
    if model_fallback:
        attempt_metadata["resume"]["model_fallback"] = model_fallback
    if recovery_context.get("transport_failure"):
        failure_count = int(previous_resume.get("transport_failure_count", 0)) + 1
        delay_seconds = min(300, retry_policy.TRANSPORT_RESUME_DELAY_SECONDS * 2 ** (failure_count - 1))
        attempt_metadata["resume"].update(
            transport_failure_count=failure_count,
            not_before_at=(values.parse_time(run_now) + timedelta(seconds=delay_seconds)).isoformat(),
        )
    existing_pr_head_sha = values.json_object(previous_metadata_json).get("existing_pr_head_sha")
    if isinstance(existing_pr_head_sha, str) and existing_pr_head_sha:
        attempt_metadata["existing_pr_head_sha"] = existing_pr_head_sha
    attempt_metadata["result_contract"] = result_contract.CONTRACT_ID
    db_write_attempts.insert_for_create_resume_attempt_and_prompt(
        conn,
        attempt_id,
        task_id,
        attempt_no,
        worktree_path,
        branch_name,
        run_now,
        run_now,
        json.dumps(attempt_metadata, ensure_ascii=False, sort_keys=True),
    )
    for artifact_type, path in [
        ("recovery_context", recovery_path),
        ("prompt", prompt_path),
        ("github_context_json", github_context["json_path"]),
        ("github_context_md", github_context["md_path"]),
    ]:
        insert_file_artifact(conn, task_id, attempt_id, artifact_type, path, run_now)
    db_write_tasks.mark_task_waiting_for_publish(conn, run_now, task_id)
    db_write_workstreams.update_for_create_task(conn, task_id, run_now, workstream_id)
    return {
        "attempt_id": attempt_id,
        "attempt_no": attempt_no,
        "prompt_path": str(prompt_path),
        "recovery_artifact_path": str(recovery_path),
    }

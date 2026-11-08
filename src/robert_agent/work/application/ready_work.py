"""Prepare work already admitted by operators or durable workflows."""

from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, RemediationStatus, TaskKind

import json
import subprocess

from robert_agent.adapters.github import discovery as discover
from robert_agent.context import attempt as attempt_context
from robert_agent.context import prompts as render_prompt
from robert_agent.delivery import contract as result_contract
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import ownership as execution_ownership
from robert_agent.execution import retry as retry_policy
from robert_agent.execution import rounds as native_rounds
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.execution.application import dispatch as dispatcher
from robert_agent.foundation import errors as run_errors
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.persistence import records as db_records
from robert_agent.persistence import transactions as db_transactions
from robert_agent.persistence.queries import scheduling
from robert_agent.persistence.queries import task_events as db_read_task_events
from robert_agent.persistence.queries import tasks as db_read_tasks
from robert_agent.persistence.queries import wakeups as db_read_wakeups
from robert_agent.persistence.queries import (
    work_item_events as db_read_work_item_events,
)
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import wakeups as db_write_wakeups
from robert_agent.security import authorization as authorize
from robert_agent.supervision import store as supervision_store
from robert_agent.work import queries as work_queries
from robert_agent.work import streams as workstream
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.work.application import preparation, source_lifecycle
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.workflows import routes as route
from robert_agent.workflows import routing as workflow_routing


def prepare_detected_local_tasks(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    prompt_paths,
    config_result=None,
):
    rows = db_read_tasks.find_for_prepare_detected_local_tasks(conn, repo_id)
    prepared = 0
    for row in rows:
        (
            task_id,
            workstream_id,
            parent_task_id,
            priority,
            routing_mode,
            requested_worker,
            task_metadata_json,
            work_item_id,
            title,
            description,
            created_by,
        ) = row
        prior_retry = values.json_object(task_metadata_json).get("preparation_retry", {})
        if prior_retry.get("next_retry_at") and values.parse_time(prior_retry["next_retry_at"]) > values.parse_time(
            run_now
        ):
            continue
        event_rows = db_read_work_item_events.find_for_prepare_detected_local_tasks(conn, work_item_id)
        local_events = [
            {
                "work_item_event_id": event_row[0],
                "event_type": event_row[1],
                "actor_kind": event_row[2],
                "actor_identity": event_row[3],
                "body": event_row[4],
                "resolves_event_id": event_row[5],
                "event_at": event_row[6],
                "metadata": values.json_object(event_row[7]),
            }
            for event_row in event_rows
        ]
        event_ids = [event["work_item_event_id"] for event in local_events]
        from robert_agent.supervision import git as supervision_git
        from robert_agent.workflows.maintenance import repair as supervision_repair

        maintenance = supervision_store.repair_task(conn, task_id)
        forced_route = supervision_repair.preparation_route(conn, task_id, config_result, repo) if maintenance else None
        normalized = {
            "origin_type": "web",
            "intent": "bug_fix"
            if maintenance
            else discover.infer_intent(title, description, "manual_operator_request"),
            "event_type": "manual_operator_request",
            "title": title,
            "body": description,
        }
        route_result = (
            workflow_routing.effective_route(config_result, repo, normalized, route_id=forced_route)
            if not maintenance or forced_route
            else None
        )
        if maintenance:
            if (
                not route_result
                or route_result["expected_output"] != supervision_repair.OUTPUT_TYPE
                or not route_result["needs_worktree"]
                or route_result.get("workspace_mode", "new_branch") != "new_branch"
                or not set(supervision_repair.ACTIONS).issubset(route_result["allowed_github_actions"])
            ):
                work_application_lifecycle.finalize_failed_task(
                    conn, task_id, workstream_id, run_now, {"status": "maintenance_route_rejected"}
                )
                work_items.record_system_event(
                    conn,
                    work_item_id,
                    event_type="execution_failed",
                    idempotency_key="prepare:" + task_id,
                    body="The current maintenance policy cannot prepare this repair.",
                    metadata={"task_id": task_id},
                    now=run_now,
                )
                db_write_wakeups.update_for_prepare_detected_local_tasks_2(conn, run_now, task_id)
                continue
            route_result["allowed_github_actions"] = list(supervision_repair.ACTIONS)
        source_reference = work_item_id.removeprefix("wi-")[:12]
        worktree_event = {
            "number": source_reference,
            "title": title,
            "base_branch": repo["default_base_branch"],
        }
        conn.commit()  # External Git I/O must not retain the SQLite writer lock.
        try:
            worktree_result = (
                supervision_git.prepare(repo, task_id, data_dir, dry_run=dry_run)
                if maintenance
                else assigned_workspace.prepare_worktree(repo, worktree_event, route_result, dry_run)
            )
        except ControlDeadlineExceeded:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            failed_at = values.utc_now()
            db_transactions.begin(conn, immediate=True)
            current = next(
                (
                    row
                    for row in db_read_tasks.find_for_prepare_detected_local_tasks(conn, repo_id)
                    if row[0] == task_id
                ),
                None,
            )
            if not current or not execution_ownership.claim_task(conn, task_id, failed_at):
                continue
            failure = retry_policy.failure_state(
                values.json_object(current[6]).get("preparation_retry", {}),
                getattr(exc, "stderr", None) or str(exc),
                failed_at,
                retryable=not isinstance(exc, FileNotFoundError),
            )
            db_write_tasks.record_local_preparation_retry(conn, task_id, json.dumps(failure))
            if failure["retryable"]:
                db_write_wakeups.defer_local_preparation(conn, task_id, failure["next_retry_at"], failed_at)
                continue
            reason = run_errors.safe_route_error(exc)["safe_error"]
            work_application_lifecycle.finalize_failed_task(
                conn, task_id, workstream_id, failed_at, {"status": "preparation_failed", "safe_error": reason}
            )
            work_items.record_system_event(
                conn,
                work_item_id,
                event_type="execution_failed",
                idempotency_key="prepare:" + task_id,
                body=reason,
                metadata={"task_id": task_id},
                now=failed_at,
            )
            db_write_wakeups.update_for_prepare_detected_local_tasks_2(conn, failed_at, task_id)
            continue
        skill_status = route_result.get("skill_status") or {"runnable": True}
        if not skill_status["runnable"] or (worktree_result and not worktree_result.get("ok", True)):
            reason = (
                "missing required route skills: " + ", ".join(skill_status["missing_required"])
                if not skill_status["runnable"]
                else worktree_result["status"]
            )
            preparation.upsert_route_decision(conn, task_id, route_result, run_now)
            db_write_tasks.set_route_id_and_expected_output_by_task_id(
                conn, route_result["route_id"], route_result["expected_output"], task_id
            )
            work_application_lifecycle.finalize_failed_task(conn, task_id, workstream_id, run_now, {"status": reason})
            work_items.record_system_event(
                conn,
                work_item_id,
                event_type="execution_failed",
                idempotency_key=f"prepare:{task_id}",
                body=reason,
                metadata={"task_id": task_id},
                now=run_now,
            )
            continue
        attempt_id = values.new_id("attempt")
        prompt_dir = data_dir / "artifacts" / task_id
        prompt_dir.mkdir(parents=True, exist_ok=True)
        context_path = prompt_dir / f"{attempt_id}.task-context.json"
        context_payload = {
            "origin_type": "web",
            "work_item_id": work_item_id,
            "repo_full_name": repo["full_name"],
            "title": title,
            "description": description,
            "created_by": created_by,
            "events": local_events,
        }
        context_path.write_text(
            json.dumps(context_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        prompt_path = prompt_dir / f"{attempt_id}.prompt.md"
        runtime_context = attempt_context.runtime_context(db_path, repo, worktree_result)
        runtime_context.update(
            {
                "origin_type": "web",
                "repo_full_name": repo["full_name"],
                "work_item_id": work_item_id,
                "work_item_event_ids": event_ids,
                "requirement": {"title": title, "description": description},
                "prior_question_summaries": [
                    event["body"]
                    for event in local_events
                    if event["event_type"] in {"operator_question", "agent_question"}
                ],
                "prior_result_summaries": [
                    event["body"] for event in local_events if event["event_type"] in {"result_recorded", "completed"}
                ],
                "operator_reply": next(
                    (
                        event["body"]
                        for event in reversed(local_events)
                        if event["event_type"] in {"user_response", "approved", "changes_requested"}
                    ),
                    "",
                ),
            }
        )
        prompt_path.write_text(
            render_prompt.render_prompt(
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "workstream_id": workstream_id,
                    "review_input_policy": route.review_input_policy(route_result["expected_output"], normalized),
                },
                route_result,
                local_events,
                runtime_context=runtime_context,
            ),
            encoding="utf-8",
        )
        preparation.upsert_route_decision(conn, task_id, route_result, run_now)
        task_metadata = values.json_object(task_metadata_json)
        task_metadata["local_source_reference"] = source_reference
        task_metadata["review_input_policy"] = route.review_input_policy(route_result["expected_output"], normalized)
        db_write_tasks.update_for_prepare_detected_local_tasks(
            conn,
            route_result["route_id"],
            route_result["expected_output"],
            priority,
            routing_mode,
            requested_worker,
            run_now,
            json.dumps(task_metadata, ensure_ascii=False, sort_keys=True),
            task_id,
        )
        db_write_attempts.insert_for_prepare_detected_local_tasks(
            conn,
            attempt_id,
            task_id,
            (worktree_result or {}).get("worktree_path"),
            (worktree_result or {}).get("branch_name"),
            run_now,
            run_now,
            json.dumps(
                {
                    "origin_type": "web",
                    "work_item_id": work_item_id,
                    "result_contract": result_contract.CONTRACT_ID,
                    **(
                        {"repair_git": worktree_result["repair_git"]}
                        if worktree_result and "repair_git" in worktree_result
                        else {}
                    ),
                }
            ),
        )
        preparation.insert_file_artifact(conn, task_id, attempt_id, "task_context", context_path, run_now)
        preparation.insert_file_artifact(conn, task_id, attempt_id, "prompt", prompt_path, run_now)
        db_write_wakeups.update_for_prepare_detected_local_tasks(conn, run_id, run_now, work_item_id, task_id)
        task_info = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "workstream_id": workstream_id,
            "prompt_path": prompt_path,
            "route_id": route_result["route_id"],
            "routing_mode": routing_mode,
            "requested_worker": requested_worker,
        }
        dispatcher.queue_dispatch(
            dispatch_queue,
            task_info,
            worktree_result,
            dispatch_budget,
            repo_id=repo_id,
            repo=repo,
        )
        prompt_paths.append(str(prompt_path))
        prepared += 1
    return prepared


def prepare_github_operator_tasks(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    prompt_paths,
    config_result=None,
):
    from robert_agent.delivery.lock import task_publication_lock
    from robert_agent.work import operator_queries as github_operator

    for pending in github_operator.pending_tasks(conn, [repo_id]):
        if dispatch_budget["remaining"] <= 0:
            break
        if github_operator.blocked(conn, pending):
            continue
        observed, source = github_operator.source_read(conn, pending)
        if not observed:
            continue
        task_id = pending["task_id"]
        conn.commit()
        with task_publication_lock(db_path, task_id) as acquired:
            if not acquired:
                continue
            current = next(iter(github_operator.pending_tasks(conn, [repo_id], task_id=task_id)), None)
            if not current or github_operator.blocked(conn, current):
                continue
            context_task = current["parent_task_id"]
            events = []
            seen = set()
            while context_task and context_task not in seen and not events:
                seen.add(context_task)
                events = work_queries.task_related_events(conn, context_task, include_consumed=True)
                parent = db_records.first(db_read_tasks.find_parent_task_id_by_task_id(conn, context_task))
                context_task = parent[0] if parent else None
            # Notification trust is derived from the current configuration,
            # never from a persisted boolean set by an earlier scanner.
            trusted = {login.casefold() for login in repo.get("trusted_actors", [])}
            for event in events:
                if event.get("event_type") == "notification":
                    event["trusted_trigger_found"] = bool(
                        event.get("authorization_lookup_complete") is not False
                        and (
                            (
                                (event.get("assigned_to") or "").casefold() == repo["github_account"].casefold()
                                and (event.get("assignment_actor_login") or "").casefold() in trusted
                            )
                            or (
                                (event.get("requested_reviewer") or "").casefold() == repo["github_account"].casefold()
                                and (event.get("requester_login") or "").casefold() in trusted
                            )
                            or (event.get("mentions_dd") and (event.get("actor_login") or "").casefold() in trusted)
                        )
                    )
            authorized = [
                event
                for event in events
                if authorize.authorize_event(event, repo, known_workstreams=set())["authorization_status"]
                in {"authorized_trigger", "accepted_context"}
            ]
            parent_row = db_records.first(
                db_read_tasks.find_task_kind_and_metadata_json_by_task_id(conn, current["parent_task_id"])
            )
            parent_episode = values.json_object(parent_row[1]).get("remediation_episode_id") if parent_row else None
            episode = remediation.load_episode(conn, parent_episode) if parent_episode else None
            operator_episode_reply = bool(
                parent_row
                and parent_row[0] in REMEDIATION_TASK_KINDS
                and episode
                and episode["status"] == RemediationStatus.EXHAUSTED
                and episode.get("metadata", {}).get("last_transition_reason") == "worker_waiting_for_user"
            )
            if operator_episode_reply and not authorized:
                # The local operator command is the fresh authority. Preserve the
                # old system event as context without reviving its ended Episode.
                authorized = list(events[:1])
            reason = None
            if not isinstance(source, dict) or "body" not in source:
                reason = "github_operator_context_unavailable"
            elif source.get("state", "").lower() != "open":
                reason = "github_operator_source_closed"
            elif not authorized:
                reason = "github_operator_authorization_unavailable"
            if reason:
                if work_application_lifecycle.finalize_failed_task(
                    conn, task_id, current["workstream_id"], run_now, {"status": reason}
                ):
                    work_items.record_system_event(
                        conn,
                        current["work_item_id"],
                        event_type="execution_failed",
                        idempotency_key="github-operator-failed:" + task_id,
                        body="GitHub operator request stopped: " + reason + ". Inspect the source and retry execution.",
                        metadata={"task_id": task_id, "reason_code": reason},
                        now=run_now,
                    )
                    db_write_wakeups.update_for_prepare_github_operator_tasks(
                        conn, run_id, run_now, current["wakeup_id"]
                    )
                continue
            responses = db_read_work_item_events.find_for_prepare_github_operator_tasks(conn, current["work_item_id"])
            operator_responses = [
                dict(zip(("event_id", "event_type", "actor_identity", "body", "created_at"), row)) for row in responses
            ]
            refreshed = []
            trigger = authorized[0]
            for event in [trigger, *(item for item in events if item is not trigger)]:
                updated = dict(event)
                if event["source_type"] == current["source_type"] and event["number"] == current["number"]:
                    updated.update(
                        title=source.get("title", event.get("title", "")),
                        source_updated_at=source.get("updated_at"),
                        source_body=source["body"] or "",
                    )
                    if event["event_type"] in {"assigned", "notification", "mention"}:
                        updated.update(
                            body=source["body"] or "", body_observed=True, body_updated_at=source.get("updated_at")
                        )
                refreshed.append(updated)
            refreshed[0]["operator_responses"] = operator_responses
            if operator_episode_reply:
                response = operator_responses[-1] if operator_responses else {"body": ""}
                refreshed[0] = {
                    **refreshed[0],
                    "task_kind": TaskKind.HUMAN_REQUEST.value,
                    "actor_kind": "operator",
                    "event_type": "manual_operator_request",
                    "body": response["body"],
                    "intent": discover.infer_intent(None, response["body"], "manual_operator_request"),
                    "metadata": {},
                    "operator_responses": operator_responses,
                }
            event = refreshed[0]
            parent_route = db_records.first(db_read_tasks.find_route_id_by_task_id(conn, current["parent_task_id"]))
            route_id = parent_route[0] if parent_route else None
            if operator_episode_reply:
                route_id = None
            elif route_id in {None, "waiting-for-user"}:
                route_id = "classification-result"
            route_result = workflow_routing.effective_route(config_result, repo, event, route_id=route_id)
            conn.commit()
            try:
                worktree_result = assigned_workspace.prepare_worktree(repo, event, route_result, dry_run)
            except ControlDeadlineExceeded:
                raise
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                worktree_result = {"ok": False, "status": "github_operator_worktree_unavailable"}
            # Cancellation and source reconciliation may run during Git I/O.
            current = next(iter(github_operator.pending_tasks(conn, [repo_id], task_id=task_id)), None)
            if (
                not current
                or github_operator.blocked(conn, current)
                or github_operator.source_read(conn, current) != (observed, source)
            ):
                continue
            info = preparation.create_task_attempt_and_prompt(
                conn,
                data_dir,
                db_path,
                repo,
                repo_id,
                event,
                route_result,
                {"workstream_id": current["workstream_id"]},
                run_now,
                worktree_result=worktree_result,
                parent_task_id=current["parent_task_id"],
                related_events=refreshed,
                existing_task_id=task_id,
            )
            if not info:
                continue
            db_write_wakeups.update_for_prepare_github_operator_tasks(conn, run_id, run_now, current["wakeup_id"])
            dispatcher.queue_dispatch(
                dispatch_queue, info, worktree_result, dispatch_budget, repo_id=repo_id, repo=repo
            )
            prompt_paths.append(str(info["prompt_path"]))


def _record_retrigger_preparation_failure(conn, wakeup_id, task_id, workstream_id, exc, now):
    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    row = db_records.first(db_read_wakeups.record_retrigger_preparation_failure(conn, wakeup_id))
    if not row or not execution_ownership.parent_allows_child(conn, task_id, retrigger_wakeup_id=wakeup_id):
        return
    metadata = values.json_object(row[0])
    failure = retry_policy.failure_state(
        metadata.get("preparation_retry", {}),
        getattr(exc, "stderr", None) or str(exc),
        now,
        retryable=not isinstance(exc, FileNotFoundError),
    )
    metadata["preparation_retry"] = failure
    db_write_wakeups.record_retrigger_preparation_failure(
        conn,
        "pending" if failure["retryable"] else "canceled",
        failure["next_retry_at"] or now,
        failure["attempt_count"],
        json.dumps(metadata),
        now,
        wakeup_id,
    )
    if not failure["retryable"]:
        execution_notifications.insert(
            conn, "github_retrigger_failed", "recorded", now, {"task_id": task_id, **failure}
        )
        item = work_items.resolve_work_item_for_workstream(conn, workstream_id)
        if item:
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="execution_failed",
                idempotency_key="retrigger-preparation:" + wakeup_id,
                body="Recovery preparation stopped. " + failure["required_action"],
                metadata={"task_id": task_id, **failure},
                now=now,
            )


def prepare_github_retrigger_tasks(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    prompt_paths,
    config_result=None,
):
    rows = db_read_wakeups.find_for_prepare_github_retrigger_tasks(conn, repo_id, run_now, run_now)
    prepared_workstreams = set()
    for wakeup_id, parent_task_id, workstream_id, parent_route_id, wakeup_metadata in rows:
        if dispatch_budget["remaining"] <= 0:
            break
        related_events = json.loads(wakeup_metadata).get(
            "refreshed_context"
        ) or work_queries.failed_parent_context_events(conn, parent_task_id)
        if not related_events:
            continue
        task_event = related_events[0]
        route_result = workflow_routing.effective_route(
            config_result,
            repo,
            task_event,
            route_id=parent_route_id,
        )
        saved_review = native_rounds.session(conn, task_id=parent_task_id)
        if saved_review:
            if not workflow_routing.native_request_allowed(saved_review, repo, config_result):
                _record_retrigger_preparation_failure(
                    conn,
                    wakeup_id,
                    parent_task_id,
                    workstream_id,
                    ValueError("invalid configuration: current native request policy rejected"),
                    run_now,
                )
                continue
            route_result = workflow_routing.effective_route(
                config_result, repo, task_event, route_id="vote" if "source_type" in saved_review else "review-pr"
            )
        try:
            conn.commit()  # External Git I/O must not retain the SQLite writer lock.
            worktree_result = assigned_workspace.prepare_worktree(repo, task_event, route_result, dry_run)
        except ControlDeadlineExceeded:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            _record_retrigger_preparation_failure(conn, wakeup_id, parent_task_id, workstream_id, exc, run_now)
            continue
        stream = {
            "workstream_id": workstream_id,
            "source_relationship": workstream.source_relationship(task_event),
        }
        task_info = preparation.create_task_attempt_and_prompt(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            task_event,
            route_result,
            stream,
            run_now,
            worktree_result=worktree_result,
            parent_task_id=parent_task_id,
            related_events=related_events,
            retrigger_wakeup_id=wakeup_id,
        )
        if task_info is None:
            db_write_wakeups.update_for_prepare_github_retrigger_tasks_2(conn, run_now, wakeup_id)
            execution_notifications.insert(
                conn,
                "github_retrigger_blocked",
                "recorded",
                run_now,
                {"task_id": parent_task_id, "reason": "workflow ownership or source no longer allows retrigger"},
            )
            continue
        db_write_wakeups.update_for_prepare_github_retrigger_tasks(conn, run_id, run_now, wakeup_id)
        dispatcher.queue_dispatch(
            dispatch_queue,
            task_info,
            worktree_result,
            dispatch_budget,
            repo_id=repo_id,
            repo=repo,
        )
        prompt_paths.append(str(task_info["prompt_path"]))
        prepared_workstreams.add(workstream_id)
    return prepared_workstreams


def prepare_queued_reviews(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    round_store=review_store,
    config_result=None,
):
    kind = "vote" if round_store is vote_store else "review"
    for round_id in scheduling.queued_round_ids(conn, kind, repo["full_name"]):
        entry = round_store.session(conn, round_id=round_id)
        if round_store is review_store:
            event_id = values.github_event_id(entry["trigger_fingerprint"])
            if db_read_task_events.find_for_prepare_queued_reviews(conn, event_id):
                round_store.update(conn, round_id, status="canceled", error="trigger_already_processed")
                continue
        if not scheduling.native_round_can_prepare(conn, kind, entry["workstream_id"]):
            continue
        event = entry["request"]["event"]
        if not workflow_routing.native_request_allowed(entry, repo, config_result):
            round_store.update(
                conn,
                round_id,
                status="canceled",
                error="current authorization or native route no longer permits this request",
            )
            execution_notifications.insert(
                conn,
                "native_request_canceled",
                "recorded",
                run_now,
                {"round_id": round_id, "reason_code": "current_policy_rejected", "repo_id": repo_id},
            )
            continue
        if source_lifecycle.record_current_state_closed_source_if_needed(
            conn,
            repo_id,
            values.github_source_id(event["source_key"]),
            values.github_event_id(event["event_fingerprint"]),
            event,
            run_now,
        ):
            round_store.update(conn, round_id, status="canceled", error="source_closed")
            continue
        task_info = preparation.create_task_attempt_and_prompt(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            event,
            entry["request"]["route"],
            {"workstream_id": entry["workstream_id"]},
            run_now,
            native_round_id=round_id,
        )
        if task_info:
            dispatcher.queue_dispatch(dispatch_queue, task_info, None, dispatch_budget, repo_id=repo_id, repo=repo)

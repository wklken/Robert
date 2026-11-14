"""Admit ordered requests and prepare available work under existing task fences."""

from robert_agent.foundation.contracts.work import FactClassification

from robert_agent.persistence import transactions as db_transactions
from robert_agent.execution.application import dispatch as dispatcher
from robert_agent.adapters.github import discovery as discover
from robert_agent.foundation import errors as run_errors
from robert_agent.execution.application import facts
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work.application import admission as work_admission
from robert_agent.work.application import dispatch_admission
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import completion
from robert_agent.work.application import github_requests
from robert_agent.work.application import preparation
from robert_agent.work.application import ready_work
from robert_agent.work.application import remediation_requests
from robert_agent.work.application import source_events
from robert_agent.work.application import source_lifecycle
from robert_agent.execution import ownership as execution_ownership
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.supervision import lineage as supervision_lineage
from robert_agent.supervision import store as supervision_store
from robert_agent.work import policies as work_policies
from robert_agent.work import queries as work_queries
from robert_agent.work import streams as workstream
from robert_agent.work import task_state
from robert_agent.workflows import routing as workflow_routing
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store
import subprocess


def admit_requests(
    conn,
    *,
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
    config_result,
    decisions,
    known_workstreams,
    active_workstreams,
    summary,
):
    admitted = dispatch_admission.prepare(
        conn,
        config=config_result,
        repo=repo,
        repo_id=repo_id,
        now=run_now,
        dispatch_queue=dispatch_queue,
        dispatch_budget=dispatch_budget,
        prompt_paths=prompt_paths,
    )
    known_workstreams.update(admitted)
    active_workstreams.update(admitted)
    if not dry_run:
        queued_prepared_count = dispatcher.queue_prepared_attempts(
            conn,
            repo_id,
            repo,
            dispatch_queue,
            min(
                dispatch_budget["remaining"],
                dispatch_budget.get("repo_remaining", {}).get(repo_id, dispatch_budget["remaining"]),
            ),
        )
        if repo_id in dispatch_budget.get("repo_remaining", {}):
            dispatch_budget["repo_remaining"][repo_id] -= queued_prepared_count
        dispatch_budget["remaining"] = max(
            0,
            dispatch_budget["remaining"] - queued_prepared_count,
        )
    if dispatch_budget["remaining"] > 0:
        active_workstreams.update(
            ready_work.prepare_github_retrigger_tasks(
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
                config_result=config_result,
            )
        )
    if dispatch_budget["remaining"] > 0:
        ready_work.prepare_github_operator_tasks(
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
            config_result,
        )
    if dispatch_budget["remaining"] > 0:
        ready_work.prepare_detected_local_tasks(
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
            config_result=config_result,
        )
    route_failures = []
    invalidation_attempt_ids = []
    failed_workstreams = set()

    track_admission = supervision_store.has_schema(conn)
    for event in decisions:
        source_id, event_id = source_events.insert_source_and_event(conn, repo_id, event, run_now)
        admission = None
        if track_admission and supervision_lineage.is_driving_github_input(event):
            # Retain acceptance before any preparation releases the writer.
            admission = supervision_lineage.record_github_received(conn, event, event_id, repo_id, source_id, run_now)
        if (
            event.get("authorization_status") == "context_only"
            and (event.get("actor_login") or "").casefold() != repo["github_account"].casefold()
            and event.get("mentions_dd")
        ):
            # Retain discussion as data without routing, resuming, or
            # refreshing the active worker's instructions.
            work_admission.append_event_to_active_task(conn, event, "context", run_now)
            continue
        if (
            event.get("authorization_status") in {"authorized_trigger", "accepted_context"}
            and work_policies.is_trusted_actor(event, repo)
            and event.get("intent") == "vote"
        ):
            if task_state.event_is_attached(conn, event_id):
                supervision_lineage.record_disposition(
                    conn,
                    admission,
                    disposition="explicitly_ignored",
                    now=run_now,
                    reason_code="trigger_already_processed",
                )
                continue  # Never reinterpret an event that already drove or informed a task.
            vote_route = workflow_routing.effective_route(config_result, repo, event)
            if vote_route.get("native_vote_workers"):
                github_requests.enqueue_native_request(
                    conn, vote_store, repo, event, vote_route, vote_route["native_vote_workers"], admission, run_now
                )
                continue
        if github_requests.resume_waiting_vote(conn, repo, event, run_now, admission):
            continue
        # New review requests are independent durable rounds. They never
        # become pending context that a still-running reviewer can consume.
        if (
            event.get("source_type") == "pull_request"
            and event.get("authorization_status") in {"authorized_trigger", "accepted_context"}
            and (
                event.get("authorization_status") == "authorized_trigger" or work_policies.is_trusted_actor(event, repo)
            )
        ):
            review_route = workflow_routing.effective_route(config_result, repo, event)
            if review_route.get("native_review_workers"):
                if task_state.event_is_attached(conn, event_id):
                    supervision_lineage.record_disposition(
                        conn,
                        admission,
                        disposition="explicitly_ignored",
                        now=run_now,
                        reason_code="trigger_already_processed",
                    )
                    continue
                github_requests.enqueue_native_request(
                    conn,
                    review_store,
                    repo,
                    event,
                    review_route,
                    review_route["native_review_workers"],
                    admission,
                    run_now,
                )
                continue
        # A consumed trigger belongs to its original task, even when the
        # workstream now points to a stopped task or a different execution.
        if source_events.should_skip_terminal_trigger(conn, event):
            supervision_lineage.record_disposition(
                conn, admission, disposition="explicitly_ignored", now=run_now, reason_code="trigger_already_processed"
            )
            continue
        workflow_fact = facts.apply_workflow_event_fact(conn, event, run_now)
        if workflow_fact:
            attempt_id = workflow_fact.get("attempt_id")
            if workflow_fact.get("classification") == FactClassification.INVALIDATING.value and attempt_id:
                invalidation_attempt_ids.append(attempt_id)
        remediation_episode_id = ((event.get("metadata") or {}).get("remediation") or {}).get("episode_id")
        if remediation_episode_id and not execution_ownership.claim_episode(
            conn,
            remediation_episode_id,
            run_now,
        ):
            supervision_lineage.record_disposition(
                conn,
                admission,
                disposition="explicitly_ignored",
                now=run_now,
                reason_code="remediation_episode_inactive",
            )
            continue
        related_workstreams = {
            event["workstream_id"],
            event.get("origin_workstream_id"),
        } - {None}
        if related_workstreams & failed_workstreams:
            failed_workstreams.update(related_workstreams)
            continue
        if work_policies.is_driving_context_event(event):
            waiting_task = source_events.waiting_task_state(conn, event["workstream_id"])
            if waiting_task:
                if not execution_ownership.waiting_reply_allowed(conn, waiting_task["task_id"], run_now):
                    continue
                if not work_policies.is_trusted_actor(event, repo):
                    work_admission.append_event_to_active_task(conn, event, "context", run_now)
                    continue
                work_items.record_github_response(
                    conn,
                    event["workstream_id"],
                    actor_identity=event.get("actor_login") or "unknown",
                    body=event.get("body") or "",
                    event_fingerprint=event["event_fingerprint"],
                    metadata={"source_key": event.get("source_key")},
                    now=run_now,
                )
                waiting_context_events = completion.pending_events_for_task(
                    conn,
                    waiting_task["task_id"],
                    [],
                )
                stream = {
                    "workstream_id": event["workstream_id"],
                    "source_relationship": workstream.source_relationship(event),
                }
                route_result = workflow_routing.effective_route(
                    config_result,
                    repo,
                    event,
                )
                try:
                    conn.commit()  # External Git I/O must not retain the SQLite writer lock.
                    worktree_result = assigned_workspace.prepare_worktree(repo, event, route_result, dry_run)
                except ControlDeadlineExceeded:
                    raise
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                    route_failures.append(
                        {
                            **run_errors.safe_route_error(exc),
                            "event_fingerprint": event["event_fingerprint"],
                            "workstream_id": event["workstream_id"],
                        }
                    )
                    failed_workstreams.update(related_workstreams)
                    continue
                if remediation_requests.handle_blocked_remediation_worktree(
                    conn,
                    event,
                    worktree_result,
                    run_now,
                ):
                    continue
                # Cancellation or a different reply can win while Git I/O
                # releases the writer. Never rewrite that stopped parent.
                if not conn.in_transaction:
                    db_transactions.begin(conn, immediate=True)
                current_waiting = source_events.waiting_task_state(conn, event["workstream_id"])
                if (
                    not current_waiting
                    or current_waiting["task_id"] != waiting_task["task_id"]
                    or not execution_ownership.parent_allows_child(conn, waiting_task["task_id"])
                ):
                    continue
                task_state.complete_waiting_parent(conn, waiting_task["task_id"], run_now)
                task_info = preparation.create_task_attempt_and_prompt(
                    conn,
                    data_dir,
                    db_path,
                    repo,
                    repo_id,
                    event,
                    route_result,
                    stream,
                    run_now,
                    worktree_result=worktree_result,
                    parent_task_id=waiting_task["task_id"],
                    related_events=[event] + waiting_context_events,
                )
                if task_info is None:
                    continue
                supervision_lineage.record_task_disposition(conn, admission, task_info["task_id"], run_now)
                waiting_vote = vote_store.session(conn, task_id=waiting_task["task_id"])
                if waiting_vote and waiting_vote["status"] == "waiting_for_user":
                    vote_store.update(
                        conn,
                        waiting_vote["round_id"],
                        status="superseded",
                        error=f"waiting vote replaced by independent {route_result['route_id']} request",
                    )
                execution_ownership.close_terminal_task(conn, waiting_task["task_id"], run_now)
                dispatcher.queue_dispatch(
                    dispatch_queue,
                    task_info,
                    worktree_result,
                    dispatch_budget,
                    repo_id=repo_id,
                    repo=repo,
                )
                prompt_paths.append(str(task_info["prompt_path"]))
                active_workstreams.add(task_info["workstream_id"])
                known_workstreams.add(task_info["workstream_id"])
                continue
            if event["workstream_id"] in active_workstreams:
                appended = work_admission.append_event_to_active_task(conn, event, "context", run_now)
                if appended:
                    if admission is not None:
                        supervision_lineage.record_task_disposition(
                            conn,
                            admission,
                            work_queries.active_task_id(conn, event["workstream_id"]),
                            run_now,
                            pending=True,
                        )
                    try:
                        refreshed_prompt = preparation.refresh_prepared_task(
                            conn,
                            data_dir,
                            db_path,
                            repo,
                            event,
                            run_now,
                            dry_run,
                            config_result=config_result,
                        )
                    except ControlDeadlineExceeded:
                        raise
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                        route_failures.append(
                            {
                                **run_errors.safe_route_error(exc),
                                "event_fingerprint": event["event_fingerprint"],
                                "workstream_id": event["workstream_id"],
                            }
                        )
                        failed_workstreams.update(related_workstreams)
                        continue
                    if refreshed_prompt:
                        prompt_paths.append(str(refreshed_prompt))
                continue
            if not (
                work_policies.is_trusted_actor(event, repo)
                or github_requests.is_dd_pr_followup_event(event)
                or event.get("authorization_status") == "accepted_review_participant"
            ):
                continue
            if source_lifecycle.record_current_state_closed_source_if_needed(
                conn,
                repo_id,
                source_id,
                event_id,
                event,
                run_now,
            ):
                summary["current_state_closed_skip_count"] += 1
                supervision_lineage.record_disposition(
                    conn, admission, disposition="explicitly_ignored", now=run_now, reason_code="source_closed"
                )
                continue
            retry_parent_id = work_queries.latest_task_id(conn, event["workstream_id"])
            if retry_parent_id and not source_events.github_followup_allows_new_subject(
                conn, repo, event, retry_parent_id
            ):
                continue
            retry_context = work_queries.failed_parent_context_events(conn, retry_parent_id)
            # A completed request's issue title describes old work. Infer
            # this new instruction on its own; ambiguous questions go to
            # classification instead of inheriting an old "bug"/"报错".
            task_event = (
                retry_context[0]
                if retry_context
                else {
                    **event,
                    "intent": discover.infer_intent(
                        None, event.get("body"), event["event_type"], repo["github_account"]
                    ),
                }
            )
            retry_related = (
                github_requests.dedupe_events_by_fingerprint(retry_context + [event]) if retry_context else None
            )
            stream = workstream.plan_event(task_event, active_workstreams=active_workstreams)
            route_result = workflow_routing.effective_route(
                config_result,
                repo,
                task_event,
            )
            try:
                conn.commit()  # External Git I/O must not retain the SQLite writer lock.
                worktree_result = assigned_workspace.prepare_worktree(repo, task_event, route_result, dry_run)
            except ControlDeadlineExceeded:
                raise
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                route_failures.append(
                    {
                        **run_errors.safe_route_error(exc),
                        "event_fingerprint": event["event_fingerprint"],
                        "workstream_id": event["workstream_id"],
                    }
                )
                failed_workstreams.update(related_workstreams)
                continue
            if remediation_requests.handle_blocked_remediation_worktree(
                conn,
                task_event,
                worktree_result,
                run_now,
            ):
                continue
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
                parent_task_id=retry_parent_id,
                related_events=retry_related,
                new_request_event=event if retry_parent_id else None,
            )
            if task_info is None:
                continue
            supervision_lineage.record_task_disposition(conn, admission, task_info["task_id"], run_now)
            dispatcher.queue_dispatch(
                dispatch_queue,
                task_info,
                worktree_result,
                dispatch_budget,
                repo_id=repo_id,
                repo=repo,
            )
            prompt_paths.append(str(task_info["prompt_path"]))
            active_workstreams.add(task_info["workstream_id"])
            known_workstreams.add(task_info["workstream_id"])
            continue
        if event["authorization_status"] not in {
            "authorized_trigger",
            "authorized_system_trigger",
        }:
            continue
        stream = workstream.plan_event(event, active_workstreams=active_workstreams)
        if stream["action"] == "append_pending_event":
            appended = work_admission.append_event_to_active_task(conn, event, "pending", run_now)
            if appended and admission is not None:
                supervision_lineage.record_task_disposition(
                    conn, admission, work_queries.active_task_id(conn, event["workstream_id"]), run_now, pending=True
                )
            continue
        if source_lifecycle.record_current_state_closed_source_if_needed(
            conn,
            repo_id,
            source_id,
            event_id,
            event,
            run_now,
        ):
            summary["current_state_closed_skip_count"] += 1
            supervision_lineage.record_disposition(
                conn, admission, disposition="explicitly_ignored", now=run_now, reason_code="source_closed"
            )
            continue
        route_result = workflow_routing.effective_route(
            config_result,
            repo,
            event,
        )
        try:
            conn.commit()  # External Git I/O must not retain the SQLite writer lock.
            worktree_result = assigned_workspace.prepare_worktree(repo, event, route_result, dry_run)
        except ControlDeadlineExceeded:
            raise
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            route_failures.append(
                {
                    **run_errors.safe_route_error(exc),
                    "event_fingerprint": event["event_fingerprint"],
                    "workstream_id": event["workstream_id"],
                }
            )
            failed_workstreams.update(related_workstreams)
            continue
        if remediation_requests.handle_blocked_remediation_worktree(
            conn,
            event,
            worktree_result,
            run_now,
        ):
            continue
        task_info = preparation.create_task_attempt_and_prompt(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            event,
            route_result,
            stream,
            run_now,
            worktree_result=worktree_result,
        )
        if task_info is None:
            continue
        supervision_lineage.record_task_disposition(conn, admission, task_info["task_id"], run_now)
        dispatcher.queue_dispatch(
            dispatch_queue,
            task_info,
            worktree_result,
            dispatch_budget,
            repo_id=repo_id,
            repo=repo,
        )
        prompt_paths.append(str(task_info["prompt_path"]))
        active_workstreams.add(task_info["workstream_id"])

    ready_work.prepare_queued_reviews(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        run_now,
        dry_run,
        dispatch_queue,
        dispatch_budget,
        config_result=config_result,
    )
    ready_work.prepare_queued_reviews(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        run_now,
        dry_run,
        dispatch_queue,
        dispatch_budget,
        round_store=vote_store,
        config_result=config_result,
    )

    if invalidation_attempt_ids:
        # Commit the classifier fence before touching any Worker process.
        conn.commit()
        facts.terminate_invalidated_attempts(db_path, invalidation_attempt_ids)
    return route_failures

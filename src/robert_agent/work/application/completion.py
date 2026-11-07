"""Finish work from accepted results and resume durable continuations."""

from robert_agent.execution.application import dispatch as dispatcher
from robert_agent.adapters.github import discovery as discover
from robert_agent.delivery import results
from robert_agent.execution import workspace as assigned_workspace
from robert_agent.work import source_policy
from robert_agent.work.application import commands as work_items
from robert_agent.work import control_queries
from robert_agent.work.application import handoff as pending_handoff
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.work.application import preparation
from robert_agent.delivery import contract as result_contract
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import ownership as execution_ownership
from robert_agent.execution import retry as retry_policy
from robert_agent.foundation import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.work import queries as work_queries
from robert_agent.work import streams as workstream
from robert_agent.work import wakeup
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.workflows import routes as route
from robert_agent.workflows import routing as workflow_routing
import json
import re
import sqlite3
import subprocess


def _mark_consumed_events(conn, task_id, consumed_fingerprints):
    for fingerprint in consumed_fingerprints:
        conn.execute(
            """
            UPDATE task_events
            SET relationship = 'consumed'
            WHERE task_id = ?
              AND event_id IN (
                SELECT event_id FROM github_events WHERE event_fingerprint = ?
              )
            """,
            (task_id, fingerprint),
        )


def pending_events_for_task(conn, task_id, consumed_fingerprints):
    consumed = set(consumed_fingerprints)
    rows = conn.execute(
        """
        SELECT ge.payload_json, ge.event_fingerprint
        FROM task_events te
        JOIN github_events ge ON ge.event_id = te.event_id
        WHERE te.task_id = ?
          AND te.relationship IN ('pending', 'context')
        ORDER BY ge.event_at, ge.event_id
        """,
        (task_id,),
    ).fetchall()
    return [json.loads(payload) for payload, fingerprint in rows if fingerprint not in consumed]


def _materialize_open_pr_workstream(conn, repo_id, repo_full_name, origin_workstream_id, action, run_now):
    match = re.search(r"/pull/(\d+)/?$", action.get("url") or "")
    if not match:
        return
    pr_number = int(match.group(1))
    pr_source_key = discover.source_key(repo_full_name, "pull_request", pr_number)
    pr_source_id = values.github_source_id(pr_source_key)
    conn.execute(
        """
        INSERT INTO github_sources(
          source_id, repo_id, source_key, source_type, number, html_url, title, state, author_login
        )
        VALUES (?, ?, ?, 'pull_request', ?, ?, '', 'open', NULL)
        ON CONFLICT(source_key) DO UPDATE SET
          html_url = excluded.html_url,
          state = 'open'
        """,
        (pr_source_id, repo_id, pr_source_key, pr_number, action.get("url")),
    )
    conn.execute(
        """
        INSERT INTO workstreams(
          workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle,
          active_task_id, created_at, updated_at, metadata_json
        )
        VALUES (?, ?, ?, ?, 'completed', NULL, ?, ?, '{}')
        ON CONFLICT(workstream_id) DO UPDATE SET
          origin_workstream_id = excluded.origin_workstream_id,
          primary_source_id = excluded.primary_source_id,
          updated_at = excluded.updated_at
        """,
        (pr_source_key, repo_id, pr_source_id, origin_workstream_id, run_now, run_now),
    )
    conn.execute(
        """
        INSERT INTO workstream_sources(workstream_id, source_id, relationship, created_at)
        VALUES (?, ?, 'derived_pr', ?)
        ON CONFLICT(workstream_id, source_id) DO NOTHING
        """,
        (pr_source_key, pr_source_id, run_now),
    )
    item = work_items.resolve_work_item_for_workstream(conn, pr_source_key)
    if item:
        work_items.record_system_event(
            conn,
            item["work_item_id"],
            event_type="pr_opened",
            idempotency_key=f"pr-opened:{pr_source_key}",
            metadata={
                "source_id": pr_source_id,
                "source_key": pr_source_key,
                "pr_number": pr_number,
                "url": action.get("url"),
            },
            now=run_now,
        )


def _save_pending_handoff(conn, task_id, state):
    conn.execute(
        "UPDATE tasks SET metadata_json=json_set(metadata_json,'$.pending_handoff',json(?)) WHERE task_id=?",
        (json.dumps(state), task_id),
    )


def _pending_handoff_current(conn, task_id, workstream_id):
    return bool(
        execution_ownership.parent_allows_child(conn, task_id)
        and conn.execute(
            """SELECT 1
        FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id
        LEFT JOIN work_items wi ON wi.workstream_id=w.workstream_id
        LEFT JOIN github_sources gs ON gs.source_id=w.primary_source_id
        WHERE t.task_id=? AND t.workstream_id=? AND t.lifecycle='completed'
          AND w.lifecycle='active' AND w.active_task_id=t.task_id
          AND wi.canceled_at IS NULL AND COALESCE(lower(gs.state),'open')!='closed'
          AND NOT EXISTS(SELECT 1 FROM tasks child WHERE child.parent_task_id=t.task_id)""",
            (task_id, workstream_id),
        ).fetchone()
    )


def recover_pending_handoffs(
    conn,
    repo_id,
    data_dir,
    db_path,
    repo,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    config_result,
):
    recovered = 0
    for task_id, stream_id in pending_handoff.ready_rows(
        conn, [repo_id], run_now, config=None if dry_run else config_result
    ):
        if not _pending_handoff_current(conn, task_id, stream_id):
            continue
        child = _create_child_task_for_pending_events(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            task_id,
            stream_id,
            pending_events_for_task(conn, task_id, []),
            run_now,
            dry_run,
            dispatch_queue,
            dispatch_budget,
            config_result=config_result,
        )
        if child:
            recovered += 1
        state = values.json_object(
            conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        ).get("pending_handoff", {})
        if state.get("status") not in {"pending", "attention"}:
            execution_ownership.close_terminal_task(conn, task_id, run_now)
    return recovered


def _create_child_task_for_pending_events(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    parent_task_id,
    workstream_id,
    pending_events,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    config_result=None,
):
    from robert_agent.supervision import actions as supervision_actions

    if parent_task_id and not execution_ownership.parent_allows_child(conn, parent_task_id):
        return None
    remediation_context = ((pending_events[0].get("metadata") or {}).get("remediation") or {}) if pending_events else {}
    if remediation_context.get("episode_id") and not execution_ownership.claim_episode(
        conn,
        remediation_context["episode_id"],
        run_now,
    ):
        return None
    if not pending_events:
        _save_pending_handoff(conn, parent_task_id, {"status": "completed", "retryable": False})
        conn.execute(
            """
            UPDATE workstreams
            SET lifecycle = 'completed', active_task_id = NULL, updated_at = ?
            WHERE workstream_id = ?
            """,
            (run_now, workstream_id),
        )
        return None

    # Saved discussion is data; only a current trusted request may drive its child.
    child_event = next(
        (
            decision
            for pending_event in pending_events
            if (
                decision := source_policy.reauthorize_saved_request(
                    pending_event,
                    repo,
                    known_workstreams=(
                        ()
                        if pending_event.get("event_type") in {"assigned", "review_request", "notification"}
                        else (workstream_id,)
                    ),
                )
            ).get("drives_execution")
        ),
        None,
    )
    if child_event is None:
        _save_pending_handoff(conn, parent_task_id, {"status": "completed", "retryable": False})
        conn.execute(
            """
            UPDATE workstreams
            SET lifecycle = 'completed', active_task_id = NULL, updated_at = ?
            WHERE workstream_id = ?
            """,
            (run_now, workstream_id),
        )
        return None
    stream = {
        "workstream_id": workstream_id,
        "source_relationship": workstream.source_relationship(child_event),
    }
    route_result = workflow_routing.effective_route(config_result, repo, child_event)
    parent = conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (parent_task_id,)).fetchone()
    metadata = values.json_object(parent[0]) if parent else {}
    handoff = metadata.get("pending_handoff") or {}
    if not handoff.get("retryable", True) or (
        handoff.get("next_retry_at")
        and retry_policy.timestamp(handoff["next_retry_at"]) > retry_policy.timestamp(run_now)
    ):
        return None
    _save_pending_handoff(conn, parent_task_id, {**handoff, "status": "pending"})
    recovery_config = None if dry_run else config_result
    gate = supervision_actions.begin(
        conn,
        task_id=parent_task_id,
        kind="handoff_resume",
        target_id=parent_task_id,
        now=run_now,
        config=recovery_config,
        native_retry=bool(handoff.get("attempt_count")),
    )
    if gate["status"] == "blocked":
        return None
    claim = gate.get("claim")
    conn.commit()  # Persist continuation ownership before external Git I/O.
    try:
        worktree_result = assigned_workspace.prepare_worktree(repo, child_event, route_result, dry_run)
    except ControlDeadlineExceeded:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if not parent_task_id:
            raise
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        checked_at = values.utc_now() if claim else run_now
        if not _pending_handoff_current(conn, parent_task_id, workstream_id) or (
            claim and not supervision_actions.current(conn, claim, config=recovery_config, now=checked_at)
        ):
            if claim:
                supervision_actions.finish(
                    conn,
                    claim,
                    outcome="failed",
                    now=checked_at,
                    receipt={"reason_code": "handoff_target_changed_after_preparation"},
                )
            return None
        failure = retry_policy.failure_state(
            handoff,
            getattr(exc, "stderr", None) or str(exc),
            run_now,
            retryable=not isinstance(exc, FileNotFoundError),
        )
        _save_pending_handoff(
            conn,
            parent_task_id,
            {**failure, "status": "pending" if failure["retryable"] else "attention"},
        )
        if claim:
            supervision_actions.finish(
                conn,
                claim,
                outcome="failed",
                now=checked_at,
                receipt={
                    "reason_code": "handoff_preparation_failed",
                    "native_retryable": failure["retryable"],
                },
            )
        if not failure["retryable"]:
            execution_notifications.insert(
                conn,
                "pending_handoff_failed",
                "recorded",
                run_now,
                {"task_id": parent_task_id, **failure},
            )
            item = work_items.resolve_work_item_for_workstream(conn, workstream_id)
            if item:
                work_items.record_system_event(
                    conn,
                    item["work_item_id"],
                    event_type="execution_failed",
                    idempotency_key="pending-handoff:" + parent_task_id + ":" + failure["first_failed_at"],
                    body="Pending request preparation stopped. " + failure["required_action"],
                    metadata={
                        "kind": "pending_handoff",
                        "task_id": parent_task_id,
                        **failure,
                    },
                    now=run_now,
                )
        return None
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    checked_at = values.utc_now() if claim else run_now
    if (parent_task_id and not _pending_handoff_current(conn, parent_task_id, workstream_id)) or (
        claim and not supervision_actions.current(conn, claim, config=recovery_config, now=checked_at)
    ):
        if claim:
            supervision_actions.finish(
                conn,
                claim,
                outcome="failed",
                now=checked_at,
                receipt={"reason_code": "handoff_target_changed_after_preparation"},
            )
        return None
    task_info = preparation.create_task_attempt_and_prompt(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        child_event,
        route_result,
        stream,
        run_now,
        worktree_result=worktree_result,
        parent_task_id=parent_task_id,
        related_events=[child_event]
        + [
            pending_event
            for pending_event in pending_events
            if pending_event.get("event_fingerprint") != child_event.get("event_fingerprint")
        ],
    )
    if task_info is None:
        if claim:
            supervision_actions.finish(
                conn,
                claim,
                outcome="failed",
                now=checked_at,
                receipt={"reason_code": "handoff_successor_not_admitted"},
            )
        return None
    _save_pending_handoff(
        conn,
        parent_task_id,
        {
            **handoff,
            "status": "completed",
            "retryable": False,
            "next_retry_at": None,
            "child_task_id": task_info["task_id"],
        },
    )
    if claim and not supervision_actions.finish(
        conn,
        claim,
        outcome="observing",
        now=checked_at,
        receipt={
            "child_task_id": task_info["task_id"],
            "event_fingerprint": child_event["event_fingerprint"],
        },
    ):
        conn.rollback()
        return None
    dispatcher.queue_dispatch(
        dispatch_queue,
        task_info,
        worktree_result,
        dispatch_budget,
        repo_id=repo_id,
        repo=repo,
    )
    return task_info


def _latest_workstream_update_branch(conn, workstream_id, exclude_task_id=None):
    params = [workstream_id]
    exclude_clause = ""
    if exclude_task_id:
        exclude_clause = "AND t.task_id != ?"
        params.append(exclude_task_id)
    row = conn.execute(
        f"""
        SELECT a.branch_name
        FROM attempts a
        JOIN tasks t ON t.task_id = a.task_id
        WHERE t.workstream_id = ?
          AND t.route_id = 'update-existing-pr'
          AND a.branch_name IS NOT NULL
          AND a.branch_name != ''
          {exclude_clause}
        ORDER BY COALESCE(a.finished_at, a.started_at, a.heartbeat_at, t.updated_at, t.created_at) DESC,
                 a.attempt_no DESC,
                 a.attempt_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return row[0] if row else ""


def _create_child_task_for_recommended_route(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    parent_task_id,
    workstream_id,
    related_events,
    recommended_route,
    branch_slug,
    consumed_event_fingerprints,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    config_result=None,
):
    if not execution_ownership.parent_allows_child(conn, parent_task_id):
        return None
    if not recommended_route:
        return None
    route_result = route.ROUTES.get(recommended_route)
    if not route_result or route_result["route_id"] == "classification-result":
        return None
    related_events = list(related_events)
    if not related_events:
        return None
    consumed_fingerprints = set(consumed_event_fingerprints or [])
    child_event = next(
        (event for event in related_events if event.get("event_fingerprint") in consumed_fingerprints),
        related_events[0],
    )
    related_events = [child_event] + [event for event in related_events if event is not child_event]
    stream = {
        "workstream_id": workstream_id,
        "source_relationship": workstream.source_relationship(child_event),
    }
    route_result = workflow_routing.effective_route(config_result, repo, child_event, recommended_route)
    worktree_event = dict(child_event)
    if branch_slug:
        worktree_event["branch_slug"] = branch_slug
    if route_result["route_id"] == "update-existing-pr" and not worktree_event.get("existing_pr_head_branch"):
        branch_name = _latest_workstream_update_branch(
            conn,
            workstream_id,
            exclude_task_id=parent_task_id,
        )
        if branch_name:
            worktree_event["existing_pr_head_branch"] = branch_name
    parent_metadata = values.json_object(
        conn.execute(
            "SELECT metadata_json FROM tasks WHERE task_id=?",
            (parent_task_id,),
        ).fetchone()[0]
    )
    handoff = parent_metadata.get("classification_handoff") or {}
    if not handoff.get("retryable", True) or (
        handoff.get("next_retry_at")
        and retry_policy.timestamp(handoff["next_retry_at"]) > retry_policy.timestamp(run_now)
    ):
        return None
    conn.execute(
        """UPDATE tasks SET metadata_json=json_set(metadata_json,
        '$.classification_handoff',json(?)) WHERE task_id=?""",
        (json.dumps({**handoff, "status": "pending"}), parent_task_id),
    )
    conn.commit()  # Persist the handoff before external Git; do not pin the writer.
    try:
        worktree_result = assigned_workspace.prepare_worktree(repo, worktree_event, route_result, dry_run)
    except ControlDeadlineExceeded:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        failure = retry_policy.failure_state(handoff, getattr(exc, "stderr", None) or str(exc), run_now)
        conn.execute(
            """UPDATE tasks SET metadata_json=json_set(metadata_json,
            '$.classification_handoff',json(?)) WHERE task_id=?""",
            (
                json.dumps(
                    {
                        **failure,
                        "status": "pending" if failure["retryable"] else "attention",
                    }
                ),
                parent_task_id,
            ),
        )
        if not failure["retryable"]:
            execution_ownership.mark_attention(conn, parent_task_id, run_now)
            execution_notifications.insert(
                conn,
                "classification_handoff_attention",
                "recorded",
                run_now,
                {"task_id": parent_task_id, **failure},
            )
        conn.commit()
        raise
    if not execution_ownership.parent_allows_child(conn, parent_task_id):
        return None
    task_info = preparation.create_task_attempt_and_prompt(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        child_event,
        route_result,
        stream,
        run_now,
        worktree_result=worktree_result,
        parent_task_id=parent_task_id,
        related_events=related_events,
    )
    if task_info is None:
        return None
    conn.execute(
        """UPDATE tasks SET metadata_json=json_set(metadata_json,
        '$.classification_handoff',json(?)) WHERE task_id=?""",
        (
            json.dumps(
                {
                    **handoff,
                    "status": "completed",
                    "retryable": False,
                    "next_retry_at": None,
                    "child_task_id": task_info["task_id"],
                }
            ),
            parent_task_id,
        ),
    )
    dispatcher.queue_dispatch(
        dispatch_queue,
        task_info,
        worktree_result,
        dispatch_budget,
        repo_id=repo_id,
        repo=repo,
    )
    return task_info


def finalize_accepted_result(
    conn,
    data_dir,
    db_path,
    repo,
    repo_id,
    payload,
    workstream_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    config_result=None,
):
    if not execution_ownership.claim_task(conn, payload["task_id"], run_now):
        return False
    work_item_row = conn.execute(
        "SELECT work_item_id, origin_type FROM work_items WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    if work_item_row is None:
        related_item = work_items.resolve_work_item_for_workstream(conn, workstream_id)
        if related_item:
            work_item_row = (related_item["work_item_id"], related_item["origin_type"])
    if payload["output_type"] == "waiting_for_user":
        _mark_consumed_events(conn, payload["task_id"], payload["consumed_event_fingerprints"])
        if work_item_row:
            question = payload.get("operator_question") or {}
            work_items.record_system_event(
                conn,
                work_item_row[0],
                event_type="operator_question",
                idempotency_key=f"result-question:{payload['result_id']}",
                body=question.get("summary", "Operator input is required."),
                metadata={
                    **question,
                    "task_id": payload["task_id"],
                    "result_id": payload["result_id"],
                },
                now=run_now,
            )
        work_application_lifecycle.mark_task_waiting_for_user(conn, payload["task_id"], workstream_id, run_now)
        subject_type, subject_id = execution_ownership.subject_for_task(conn, payload["task_id"])
        if subject_type == "remediation_episode":
            remediation.exhaust_episode(
                conn,
                episode_id=subject_id,
                reason="worker_waiting_for_user",
                now=run_now,
            )
        return True
    for action in payload["planned_github_actions"]:
        if action.get("type") == "open_pr":
            _materialize_open_pr_workstream(
                conn,
                repo_id,
                repo["full_name"],
                workstream_id,
                action,
                run_now,
            )
    related_events = work_queries.task_related_events(
        conn,
        payload["task_id"],
        include_consumed=payload["output_type"] == "classification_result",
    )
    _mark_consumed_events(conn, payload["task_id"], payload["consumed_event_fingerprints"])
    pending_events = pending_events_for_task(
        conn,
        payload["task_id"],
        payload["consumed_event_fingerprints"],
    )
    conn.execute(
        "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
        (run_now, payload["task_id"]),
    )
    if work_item_row and work_item_row[1] == "web" and payload["output_type"] == "local_result":
        work_items.record_system_event(
            conn,
            work_item_row[0],
            event_type="result_recorded",
            idempotency_key=f"result:{payload['result_id']}",
            body=payload.get("handoff", ""),
            metadata={"task_id": payload["task_id"], "result_id": payload["result_id"]},
            now=run_now,
        )
        conn.execute(
            """
            UPDATE work_items
            SET completed_at = ?, updated_at = ?, version = version + 1
            WHERE work_item_id = ? AND completed_at IS NULL
            """,
            (run_now, run_now, work_item_row[0]),
        )
    if payload["output_type"] == "classification_result":
        child_task = _create_child_task_for_recommended_route(
            conn,
            data_dir,
            db_path,
            repo,
            repo_id,
            payload["task_id"],
            workstream_id,
            related_events,
            payload.get("recommended_route"),
            payload.get("branch_slug"),
            payload.get("consumed_event_fingerprints"),
            run_now,
            dry_run,
            dispatch_queue,
            dispatch_budget,
            config_result=config_result,
        )
        if child_task:
            if work_item_row:
                decision = conn.execute(
                    """
                    SELECT e.event_id
                    FROM work_item_events e
                    WHERE e.work_item_id = ?
                      AND e.event_type = 'operator_decision_required'
                      AND NOT EXISTS (
                        SELECT 1 FROM work_item_events response
                        WHERE response.resolves_event_id = e.event_id
                      )
                    ORDER BY e.created_at DESC, e.event_id DESC
                    LIMIT 1
                    """,
                    (work_item_row[0],),
                ).fetchone()
                if decision:
                    work_items.record_system_event(
                        conn,
                        work_item_row[0],
                        event_type="routing_selected",
                        idempotency_key=f"classification-routing:{payload['result_id']}",
                        resolves_event_id=decision[0],
                        metadata={
                            "result_id": payload["result_id"],
                            "task_id": child_task["task_id"],
                            "route_id": child_task["route_id"],
                        },
                        now=run_now,
                    )
                    conn.execute(
                        """
                        UPDATE work_items
                        SET activated_at = COALESCE(activated_at, ?),
                            updated_at = ?,
                            version = version + 1
                        WHERE work_item_id = ?
                        """,
                        (run_now, run_now, work_item_row[0]),
                    )
            execution_ownership.close_terminal_task(conn, payload["task_id"], run_now)
            return True
    _create_child_task_for_pending_events(
        conn,
        data_dir,
        db_path,
        repo,
        repo_id,
        payload["task_id"],
        workstream_id,
        pending_events,
        run_now,
        dry_run,
        dispatch_queue,
        dispatch_budget,
        config_result=config_result,
    )
    handoff = values.json_object(
        conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (payload["task_id"],)).fetchone()[0]
    ).get("pending_handoff", {})
    if handoff.get("status") not in {"pending", "attention"}:
        execution_ownership.close_terminal_task(conn, payload["task_id"], run_now)
    return True


def _orphaned_classification_rows(conn, repo_ids, now):
    if not repo_ids:
        return []
    placeholders = ",".join("?" for _ in repo_ids)
    return conn.execute(
        f"""
        SELECT
          wr.result_id,
          wr.task_id,
          wr.attempt_id,
          wr.output_type,
          wr.consumed_event_fingerprints_json,
          wr.verification_json,
          wr.handoff,
          wr.metadata_json,
          t.workstream_id
        FROM worker_results wr
        JOIN tasks t ON t.task_id = wr.task_id
        JOIN workstreams w ON w.workstream_id = t.workstream_id
        WHERE w.repo_id IN ({placeholders})
          AND w.lifecycle = 'active'
          AND w.active_task_id = t.task_id
          AND t.lifecycle = 'completed'
          AND wr.output_type = 'classification_result'
          AND COALESCE(json_extract(t.metadata_json,'$.classification_handoff.retryable'),1)!=0
          AND (json_extract(t.metadata_json,'$.classification_handoff.next_retry_at') IS NULL
               OR julianday(json_extract(t.metadata_json,'$.classification_handoff.next_retry_at'))<=julianday(?))
          AND NOT EXISTS (
            SELECT 1
            FROM tasks child
            WHERE child.parent_task_id = t.task_id
          )
        ORDER BY wr.created_at, wr.result_id
        """,
        [*repo_ids, now],
    ).fetchall()


def has_orphaned_classification_result(conn, repo_ids, now):
    for row in _orphaned_classification_rows(conn, repo_ids, now):
        if (
            execution_ownership.parent_allows_child(conn, row[1])
            and results.load_result_payload(conn, row[:8]).get("recommended_route")
            and results.result_audit_accepted(conn, row[0])
            and results.result_actions_all_published(conn, row[0])
        ):
            return True
    return False


def recover_orphaned_classification_results(
    conn,
    repo_id,
    data_dir,
    db_path,
    repo,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    config_result=None,
):
    rows = _orphaned_classification_rows(conn, [repo_id], run_now)
    recovered = 0
    for row in rows:
        result_row = row[:8]
        if not execution_ownership.parent_allows_child(conn, row[1]):
            continue
        workstream_id = row[8]
        if values.json_object(result_row[7]).get("result_contract") != result_contract.CONTRACT_ID:
            # Old results remain audit history or unresolved cutover work. Never
            # consume, publish or terminalize them through the new protocol.
            continue
        payload = results.load_result_payload(conn, result_row)
        if not payload.get("recommended_route"):
            continue
        if not results.result_audit_accepted(conn, payload["result_id"]):
            continue
        if not results.result_actions_all_published(conn, payload["result_id"]):
            continue
        wakeup.consume_wakeups_for_results(
            conn,
            result_ids=[payload["result_id"]],
            run_id=run_id,
            now=run_now,
        )
        before_children = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?",
            (payload["task_id"],),
        ).fetchone()[0]
        try:
            finalize_accepted_result(
                conn,
                data_dir,
                db_path,
                repo,
                repo_id,
                payload,
                workstream_id,
                run_now,
                dry_run,
                dispatch_queue,
                dispatch_budget,
                config_result=config_result,
            )
        except ControlDeadlineExceeded:
            raise
        except subprocess.TimeoutExpired:
            continue
        after_children = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?",
            (payload["task_id"],),
        ).fetchone()[0]
        if after_children > before_children:
            recovered += 1
    return recovered


def _complete_published_analysis_items(conn, repo_id, run_now):
    cursor = conn.execute(
        """
        SELECT wi.*, t.task_id AS analysis_task_id, wr.result_id AS analysis_result_id
        FROM work_items wi
        JOIN github_sources gs ON gs.source_id=wi.origin_source_id
        JOIN workstreams w ON w.workstream_id=wi.workstream_id
        JOIN tasks t ON t.workstream_id=w.workstream_id
        JOIN worker_results wr ON wr.task_id=t.task_id
        WHERE wi.repo_id=? AND wi.origin_type='github'
          AND wi.completed_at IS NULL AND wi.canceled_at IS NULL
          AND gs.source_type='issue' AND lower(gs.state)='open'
          AND w.lifecycle='completed' AND w.active_task_id IS NULL
          AND t.lifecycle='completed' AND wr.output_type='comment_analysis'
          AND json_extract(wr.metadata_json,'$.audit.status')='accepted'
          AND EXISTS (SELECT 1 FROM github_actions ga WHERE ga.result_id=wr.result_id
            AND ga.action_type='comment' AND ga.audit_status='accepted' AND ga.publish_status='published')
          AND NOT EXISTS (SELECT 1 FROM github_actions ga WHERE ga.result_id=wr.result_id
            AND (ga.audit_status!='accepted' OR ga.publish_status!='published'))
          AND NOT EXISTS (SELECT 1 FROM workstreams related
            JOIN github_sources pr ON pr.source_id=related.primary_source_id
            WHERE (related.workstream_id=w.workstream_id OR related.origin_workstream_id=w.workstream_id)
              AND pr.source_type='pull_request')
          AND NOT EXISTS (SELECT 1 FROM tasks newer WHERE newer.workstream_id=t.workstream_id
            AND (newer.created_at, newer.task_id)>(t.created_at, t.task_id))
          AND NOT EXISTS (SELECT 1 FROM task_events te WHERE te.task_id=t.task_id
            AND te.relationship IN ('pending','context'))
        """,
        (repo_id,),
    )
    columns = [column[0] for column in cursor.description]
    rows = cursor.fetchall()
    completed = 0
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        for row in rows:
            item = dict(zip(columns, row))
            facts = control_queries.lifecycle_facts(conn, item)
            current = work_items._active_or_latest_task(conn, item["workstream_id"])
            if not current or current["task_id"] != item["analysis_task_id"]:
                continue
            if facts["running"] or facts["actions"] or facts["cleanup_pending"] or facts["workflow_attention"]:
                continue
            if work_items._unresolved_event(
                conn,
                item["work_item_id"],
                work_items.QUESTION_EVENTS | work_items.DECISION_EVENTS,
            ):
                continue
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="completed",
                idempotency_key=f"analysis-completed:{item['analysis_result_id']}",
                body="Analysis result was accepted and published to GitHub.",
                metadata={
                    "task_id": item["analysis_task_id"],
                    "result_id": item["analysis_result_id"],
                },
                now=run_now,
            )
            conn.execute(
                """UPDATE work_items SET completed_at=?, updated_at=?, version=version+1
                WHERE work_item_id=? AND completed_at IS NULL AND canceled_at IS NULL""",
                (run_now, run_now, item["work_item_id"]),
            )
            completed += 1
        return completed
    finally:
        conn.row_factory = previous_row_factory


def finalize_published_results(
    conn,
    repo_id,
    data_dir,
    db_path,
    repo,
    run_id,
    run_now,
    dry_run,
    dispatch_queue,
    dispatch_budget,
    config_result=None,
):
    rows = conn.execute(
        """
        SELECT
          wr.result_id,
          wr.task_id,
          wr.attempt_id,
          wr.output_type,
          wr.consumed_event_fingerprints_json,
          wr.verification_json,
          wr.handoff,
          wr.metadata_json,
          t.workstream_id
        FROM worker_results wr
        JOIN tasks t ON t.task_id = wr.task_id
        JOIN attempts a ON a.attempt_id = wr.attempt_id
        WHERE t.workstream_id IN (
          SELECT workstream_id FROM workstreams WHERE repo_id = ?
        )
          AND t.lifecycle IN ('queued', 'running')
          AND a.status = 'completed'
          AND (json_type(a.metadata_json,'$.dispatch.pid') IS NOT 'integer'
               OR json_extract(a.metadata_json,'$.process_stopped_at') IS NOT NULL)
        ORDER BY wr.created_at, wr.result_id
        """,
        (repo_id,),
    ).fetchall()
    finalized = 0
    for row in rows:
        result_row = row[:8]
        if not execution_ownership.claim_task(conn, row[1], run_now):
            continue
        workstream_id = row[8]
        if values.json_object(result_row[7]).get("result_contract") != result_contract.CONTRACT_ID:
            # Old results remain audit history or unresolved cutover work. Never
            # consume, publish or terminalize them through the new protocol.
            continue
        payload = results.load_result_payload(conn, result_row)
        if not results.result_audit_accepted(conn, payload["result_id"]):
            continue
        if work_application_lifecycle.finalize_superseded_result(conn, payload["result_id"], run_now):
            finalized += 1
            continue
        if not results.result_actions_all_published(conn, payload["result_id"]):
            continue
        wakeup.consume_wakeups_for_results(
            conn,
            result_ids=[payload["result_id"]],
            run_id=run_id,
            now=run_now,
        )
        try:
            finalize_accepted_result(
                conn,
                data_dir,
                db_path,
                repo,
                repo_id,
                payload,
                workstream_id,
                run_now,
                dry_run,
                dispatch_queue,
                dispatch_budget,
                config_result=config_result,
            )
        except ControlDeadlineExceeded:
            raise
        except subprocess.TimeoutExpired:
            continue
        finalized += 1
    _complete_published_analysis_items(conn, repo_id, run_now)
    return finalized

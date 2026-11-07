"""Pure supervision rules over a bounded snapshot of controller and task facts."""

from datetime import datetime, timedelta, timezone
import hashlib
import json


ACTIVE_TASKS = {"detected", "authorized", "classified", "queued", "running"}
TERMINAL_TASKS = {"completed", "failed", "canceled", "ignored"}
RUNNING_ATTEMPTS = {"running", "stale"}


def timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _old_enough(now, value, seconds):
    when = timestamp(value)
    return when is not None and now >= when + timedelta(seconds=seconds)


def _future(now, value):
    when = timestamp(value)
    return when if when is not None and when > now else None


def _observation(
    scope_type,
    scope_id,
    rule,
    facts,
    now,
    poll,
    classification,
    reason,
    evidence,
    expected,
    *,
    next_at=None,
    positive=None,
    progress=None,
    progress_at=None,
    eligible_since=None,
):
    if positive:
        occurred_at = timestamp(positive.get("occurred_at") or progress_at)
        if occurred_at is None or occurred_at > now:
            classification, reason, positive = (
                "unobservable",
                "progress_occurrence_time_unverified",
                None,
            )
        else:
            positive = {**positive, "occurred_at": occurred_at.isoformat()}
    row = {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "rule_id": rule,
        "lineage_key": facts.get("lineage_key"),
        "classification": classification,
        "reason_code": reason,
        "severity": "error"
        if classification == "confirmed"
        else ("warning" if classification in {"suspect", "unobservable"} else "info"),
        "evidence": evidence,
        "expected_progress": expected,
        "next_check_at": (next_at or now + timedelta(seconds=poll)).isoformat(),
        "progress_token": _digest(progress if progress is not None else positive)
        if progress or positive
        else None,
        "eligible_since": eligible_since,
    }
    if progress_at:
        row["progress_observed_at"] = progress_at
    if facts.get("generation"):
        row["generation"] = facts["generation"]
    if positive:
        row["positive_evidence"] = positive
    return row


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _eligible(
    facts,
    rule,
    emit,
    now,
    stall,
    reason,
    evidence,
    expect,
    generation,
    *,
    consumer=None,
    confirm=False,
    **kwargs,
):
    """Start at the first eligible sample, not at historical task creation."""
    checkpoint = facts.get("checkpoints", {}).get(rule, {})
    key = _digest(generation)
    start = timestamp(checkpoint.get("eligible_since"))
    if (
        start is None
        or start > now
        or checkpoint.get("evidence", {}).get("eligibility_key") != key
    ):
        start = now
    classification = "expected_wait"
    opportunity = timestamp(consumer)
    if _old_enough(now, start, stall):
        classification = (
            "confirmed"
            if confirm and opportunity and start < opportunity <= now
            else "suspect"
        )
    return emit(
        classification,
        reason,
        {**evidence, "eligibility_key": key},
        expect,
        eligible_since=start.isoformat(),
        **kwargs,
    )


def _stage_wait(facts, consumer):
    if (
        facts.get("lifecycle") == "waiting_for_user"
        or facts.get("stream_lifecycle") == "waiting_for_user"
    ):
        return "waiting_for_user"
    if facts.get("source_closed") or facts.get("canceled"):
        return "request_no_longer_active"
    if not facts.get("runnable"):
        return "execution_attention"
    return facts.get(consumer + "_wait")


def _worker(facts, emit, now, grace):
    lifecycle, status = facts["lifecycle"], facts.get("attempt_status")
    process = facts.get("process_status")
    evidence = {
        "attempt_id": facts.get("attempt_id"),
        "attempt_status": status,
        "process_status": process,
        "task_lifecycle": lifecycle,
    }
    expect = {"kind": "worker_reconciled", "attempt_id": facts.get("attempt_id")}
    if facts.get("process_stopped_at") and status not in RUNNING_ATTEMPTS:
        if lifecycle in {"canceled", "ignored"}:
            return emit("expected_wait", "request_no_longer_active", evidence, expect)
        return emit(
            "healthy",
            "worker_stop_recorded",
            evidence,
            expect,
            positive={
                "attempt_id": facts["attempt_id"],
                "process_stopped_at": facts["process_stopped_at"],
            },
            progress_at=facts["process_stopped_at"],
        )
    if process == "alive_same":
        if lifecycle in TERMINAL_TASKS:
            if not _old_enough(now, facts.get("task_updated_at"), grace):
                return emit("expected_wait", "worker_cleanup_grace", evidence, expect)
            return emit(
                "confirmed", "terminal_task_worker_still_alive", evidence, expect
            )
        return emit("healthy", "worker_identity_verified", evidence, expect)
    if process == "group_alive":
        return emit("expected_wait", "worker_group_still_alive", evidence, expect)
    if process == "dead" and status in RUNNING_ATTEMPTS:
        if facts.get("result_id") or facts.get("spool_status") == "saved":
            return emit(
                "expected_wait", "worker_exit_with_durable_result", evidence, expect
            )
        if facts.get("spool_status") != "missing":
            return emit(
                "unobservable", "worker_result_spool_unverified", evidence, expect
            )
        # A worker can exit just before normal supervision classifies it.
        anchor = facts.get("heartbeat_at") or facts.get("started_at")
        if not _old_enough(now, anchor, grace):
            return emit(
                "suspect", "worker_exit_pending_reconciliation", evidence, expect
            )
        return emit("confirmed", "worker_exited_without_result", evidence, expect)
    if (
        status in RUNNING_ATTEMPTS
        or facts.get("process_recorded")
        or lifecycle == "running"
    ):
        return emit("unobservable", "worker_identity_unverified", evidence, expect)
    return None


def _result(facts, emit, now, stall):
    result_id, spool = facts.get("result_id"), facts.get("spool_status")
    if not result_id and spool not in {"saved", "unverified"}:
        return None
    evidence = {
        "attempt_id": facts.get("attempt_id"),
        "result_id": result_id,
        "audit_status": facts.get("audit_status"),
        "spool_status": spool,
    }
    if not result_id and facts.get("spool_fingerprint"):
        evidence["spool_fingerprint"] = facts["spool_fingerprint"]
    expect = {
        "kind": "result_audited" if result_id else "result_ingested",
        "attempt_id": facts.get("attempt_id"),
        "result_id": result_id,
    }
    wait = _stage_wait(facts, "local")
    if (
        facts.get("canceled")
        or facts.get("source_closed")
        or facts["lifecycle"] in {"canceled", "ignored"}
    ):
        return emit("expected_wait", "request_no_longer_active", evidence, expect)
    if not result_id:
        if spool == "unverified":
            return emit(
                "unobservable", "worker_result_spool_unverified", evidence, expect
            )
        if wait:
            return emit("expected_wait", wait, evidence, expect)
        return _eligible(
            facts,
            "result_not_consumed",
            emit,
            now,
            stall,
            "result_waiting_ingestion",
            evidence,
            expect,
            [expect, facts.get("spool_fingerprint"), facts.get("spool_saved_at")],
            consumer=facts.get("local_completed_at"),
            confirm=True,
        )
    if facts.get("attempt_status") != "completed" or (
        facts.get("process_recorded") and not facts.get("process_stopped_at")
    ):
        return emit("expected_wait", "result_waiting_worker_exit", evidence, expect)
    if facts.get("audit_status") == "accepted":
        return emit(
            "healthy",
            "result_audit_accepted",
            evidence,
            expect,
            positive={"result_id": result_id, "audit_status": "accepted"},
            progress_at=facts.get("audited_at"),
        )
    if (
        facts.get("audit_status") in {"failed", "policy_violation"}
        or facts.get("audit_retry_required") == result_id
        or facts.get("audit_retryable") == 0
    ):
        return emit(
            "confirmed",
            "result_audit_requires_attention",
            {**evidence, "audit_reason": facts.get("audit_reason")},
            expect,
        )
    if facts.get("audit_status"):
        return emit("unobservable", "result_audit_status_unverified", evidence, expect)
    retry = _future(now, facts.get("audit_retry_at"))
    if retry:
        return emit(
            "expected_wait", "result_audit_backoff", evidence, expect, next_at=retry
        )
    if wait or facts["lifecycle"] not in ACTIVE_TASKS:
        return emit(
            "expected_wait", wait or "request_no_longer_active", evidence, expect
        )
    return _eligible(
        facts,
        "result_not_consumed",
        emit,
        now,
        stall,
        "result_waiting_audit",
        evidence,
        expect,
        [expect, facts.get("audit_eligible_at")],
        consumer=facts.get("local_completed_at"),
        confirm=True,
    )


def _publication(facts, emit, now, stall):
    action = facts.get("pending_action_id")
    result_id = facts.get("result_id")
    if not action and not facts.get("publication_receipt"):
        return None
    evidence = {
        "result_id": result_id,
        "action_id": action,
        "intent_status": facts.get("intent_status"),
    }
    expect = {
        "kind": "publication_confirmed",
        "result_id": result_id,
        "action_id": action,
    }
    if facts.get("canceled") or facts["lifecycle"] in {"canceled", "ignored"}:
        return emit("expected_wait", "request_no_longer_active", evidence, expect)
    if not action and facts.get("publication_receipt"):
        return emit(
            "healthy",
            "publication_confirmed",
            evidence,
            expect,
            positive={"result_id": result_id, **facts["publication_receipt"]},
        )
    if (
        facts.get("publication_terminal_failure")
        or facts.get("intent_status") == "attention"
    ):
        return emit(
            "confirmed",
            "publication_requires_attention",
            {**evidence, "publish_reason": facts.get("publish_reason")},
            expect,
        )
    retry = _future(now, facts.get("publication_retry_at"))
    if retry:
        return emit(
            "expected_wait", "publication_backoff", evidence, expect, next_at=retry
        )
    if facts.get("intent_status") == "executing":
        overdue = _old_enough(now, facts.get("intent_started_at"), stall)
        return emit(
            "confirmed" if overdue else "expected_wait",
            "publication_effect_unresolved",
            {**evidence, "intent_started_at": facts.get("intent_started_at")},
            expect,
        )
    wait = _stage_wait(facts, "publish")
    if (
        wait
        or not facts.get("publication_eligible")
        or facts["lifecycle"] not in ACTIVE_TASKS
    ):
        return emit(
            "expected_wait", wait or "publication_not_eligible", evidence, expect
        )
    return _eligible(
        facts,
        "publication_stalled",
        emit,
        now,
        stall,
        "publication_waiting_consumer",
        evidence,
        expect,
        [expect, facts.get("action_eligible_at")],
        consumer=facts.get("publish_completed_at"),
        confirm=True,
    )


def _handoff(facts, emit, now, stall):
    status = facts.get("handoff_status")
    if not status:
        return None
    child = facts.get("successor_task_id")
    evidence = {"handoff_status": status, "successor_task_id": child}
    expect = {"kind": "handoff_successor"}
    if (
        facts.get("canceled")
        or facts.get("source_closed")
        or facts["lifecycle"] in {"canceled", "ignored"}
    ):
        return emit("expected_wait", "request_no_longer_active", evidence, expect)
    if status == "completed" and child:
        return emit(
            "healthy",
            "handoff_successor_recorded",
            evidence,
            expect,
            positive={"successor_task_id": child},
            progress_at=facts.get("successor_occurred_at"),
        )
    if status == "attention":
        return emit("confirmed", "handoff_requires_attention", evidence, expect)
    if status == "completed":
        return emit("healthy", "handoff_no_successor_recorded", evidence, expect)
    retry = _future(now, facts.get("handoff_retry_at"))
    if retry:
        return emit("expected_wait", "handoff_backoff", evidence, expect, next_at=retry)
    wait = _stage_wait(facts, "local")
    if wait or not facts.get("handoff_current"):
        return emit("expected_wait", wait or "handoff_not_eligible", evidence, expect)
    return _eligible(
        facts,
        "handoff_stalled",
        emit,
        now,
        stall,
        "handoff_waiting_consumer",
        evidence,
        expect,
        [expect, facts.get("handoff_due_at")],
        consumer=facts.get("local_completed_at"),
        confirm=True,
    )


def _ready(facts, emit, now, stall):
    attempt = facts.get("attempt_id")
    evidence = {"attempt_id": attempt, "attempt_status": facts.get("attempt_status")}
    expect = {"kind": "attempt_dispatched", "attempt_id": attempt}
    if facts.get("canceled"):
        return None
    delivery = facts.get("delivery_positive")
    if (
        facts["lifecycle"] == "completed"
        and facts.get("attempt_status") == "completed"
        and delivery
        and (not facts.get("process_recorded") or facts.get("process_stopped_at"))
    ):
        return emit(
            "healthy",
            "prepared_attempt_delivery_verified",
            evidence,
            expect,
            positive={**delivery, "attempt_id": attempt},
            progress_at=delivery["occurred_at"],
        )
    if facts["lifecycle"] not in ACTIVE_TASKS:
        return None
    if (
        facts.get("attempt_status") in RUNNING_ATTEMPTS
        and facts.get("process_status") == "alive_same"
    ):
        started = facts.get("worker_started_at")
        return emit(
            "healthy",
            "prepared_attempt_dispatched",
            evidence,
            expect,
            positive={"attempt_id": attempt, "worker_started_at": started},
            progress_at=started,
        )
    if facts.get("attempt_status") != "prepared":
        return None
    wait = _stage_wait(facts, "local") or facts.get("dispatch_block")
    if wait:
        return emit("expected_wait", wait, evidence, expect)
    retry = _future(now, facts.get("dispatch_not_before"))
    if retry:
        return emit(
            "expected_wait", "attempt_not_before", evidence, expect, next_at=retry
        )
    if not facts.get("has_prompt") or not facts.get("has_worktree"):
        return emit("unobservable", "attempt_preparation_unverified", evidence, expect)
    return _eligible(
        facts,
        "ready_not_dispatched",
        emit,
        now,
        stall,
        "ready_attempt_waiting_dispatch",
        {
            **evidence,
            "local_daemon_run_id": facts.get("dispatch_consumer_run_id"),
            "local_completed_at": facts.get("dispatch_completed_at"),
        },
        expect,
        [
            expect,
            facts.get("dispatch_not_before"),
            facts.get("dispatch_consumer_run_id"),
        ],
        consumer=facts.get("dispatch_completed_at"),
        confirm=True,
    )


def _worker_progress(facts, emit, now, stall):
    attempt = facts.get("attempt_id")
    evidence = {
        "attempt_id": attempt,
        "result_id": facts.get("result_id"),
        "milestones": facts.get("milestones", []),
        "command_failures": facts.get("command_failures", 0),
        "command_observation": facts.get("command_observation", "limited"),
    }
    expect = {"kind": "new_worker_milestone", "attempt_id": attempt}
    if (
        facts.get("canceled")
        or facts.get("source_closed")
        or facts["lifecycle"] in {"canceled", "ignored"}
    ):
        return None
    if facts.get("result_id"):
        return emit(
            "healthy",
            "worker_result_received",
            evidence,
            expect,
            positive={"result_id": facts["result_id"], "attempt_id": attempt},
            progress_at=facts.get("result_created_at"),
        )
    if (
        facts.get("attempt_status") not in RUNNING_ATTEMPTS
        or facts.get("process_status") != "alive_same"
    ):
        return None
    milestones = facts.get("milestones") or None
    progress_at = facts.get("milestone_observed_at") if milestones else None
    wait = _stage_wait(facts, "local")
    if wait or facts.get("command_in_progress"):
        return emit(
            "expected_wait",
            wait or "tracked_command_in_progress",
            evidence,
            expect,
            progress=milestones,
            progress_at=progress_at,
        )
    return _eligible(
        facts,
        "worker_no_progress",
        emit,
        now,
        stall,
        "worker_milestone_unchanged",
        evidence,
        expect,
        [expect, milestones],
        progress=milestones,
        progress_at=progress_at,
    )


def _delivery(facts, emit, _now, _stall):
    if facts["lifecycle"] != "completed" or not facts.get("delivery_status"):
        return None
    status = facts["delivery_status"]
    evidence = {
        "result_id": facts.get("result_id"),
        "expected_output": facts.get("expected_output"),
        "output_type": facts.get("output_type"),
        "planned_action_count": facts.get("planned_action_count"),
        "action_count": facts.get("action_count"),
    }
    expect = {"kind": "delivery_confirmed", "result_id": facts.get("result_id")}
    return emit(
        {
            "inconsistent": "confirmed",
            "unknown": "unobservable",
            "retired": "expected_wait",
            "confirmed": "healthy",
        }[status],
        facts["delivery_reason"],
        evidence,
        expect,
        positive=facts.get("delivery_positive"),
    )


def _execution_failure(facts, emit, now, grace):
    recovery = facts.get("execution_recovery_evidence")
    if (
        recovery
        and not facts.get("canceled")
        and facts["lifecycle"] in {"completed", "failed"}
    ):
        evidence = {
            "previous_attempt_id": facts.get("attempt_id"),
            "successor_task_id": recovery["successor_task_id"],
        }
        return emit(
            "healthy",
            "same_request_delivery_verified",
            evidence,
            {"kind": "execution_progress_after_failure"},
            positive=recovery,
        )
    if (
        facts.get("canceled")
        or facts.get("source_closed")
        or facts["lifecycle"] != "failed"
        or facts.get("audit_status") in {"failed", "policy_violation"}
        or facts.get("audit_retry_required")
        or facts.get("publication_terminal_failure")
        or facts.get("handoff_status") == "attention"
    ):
        return None
    evidence = {
        "attempt_id": facts.get("attempt_id"),
        "result_id": facts.get("result_id"),
        "failure_status": facts.get("attempt_failure"),
        "attempt_status": facts.get("attempt_status"),
    }
    expect = {
        "kind": "execution_progress_after_failure",
        "attempt_id": facts.get("attempt_id"),
    }
    if facts.get("attempt_failure") == "failed_timeout" and not _old_enough(
        now, facts.get("finished_at"), grace
    ):
        return emit("expected_wait", "worker_recovery_grace", evidence, expect)
    return emit("confirmed", "execution_failed", evidence, expect)


def evaluate_task(
    facts, *, now, poll_seconds=60, stall_seconds=300, worker_grace_seconds=60
):
    now = timestamp(now)
    rules = (
        ("worker_state_mismatch", _worker, worker_grace_seconds),
        ("result_not_consumed", _result, stall_seconds),
        ("publication_stalled", _publication, stall_seconds),
        ("handoff_stalled", _handoff, stall_seconds),
        ("ready_not_dispatched", _ready, stall_seconds),
        ("worker_no_progress", _worker_progress, stall_seconds),
        ("delivery_inconsistent", _delivery, stall_seconds),
        ("execution_failed", _execution_failure, worker_grace_seconds),
    )
    rows = []
    for rule, classify, age in rules:

        def emit(classification, reason, evidence, expected, **kwargs):
            return _observation(
                "task",
                facts["task_id"],
                rule,
                facts,
                now,
                poll_seconds,
                classification,
                reason,
                evidence,
                expected,
                **kwargs,
            )

        row = classify(facts, emit, now, age)
        if row:
            rows.append(row)
    return rows


def evaluate_admission(facts, *, now, poll_seconds=60, stall_seconds=300):
    """Admission receipts are producer-owned. Emit separate observation rules."""
    now = timestamp(now)
    evidence = {
        key: facts.get(key)
        for key in (
            "input_generation",
            "received_at",
            "disposition",
            "target_type",
            "target_id",
            "consumer",
            "due_at",
            "consumer_completed_at",
            "successor_task_id",
        )
    }
    expect = {
        "kind": "admission_disposed",
        "input_generation": facts.get("input_generation"),
    }
    rule = "accepted_input_unowned"

    def emit(classification, reason, evidence, expected, **kwargs):
        return _observation(
            facts["scope_type"],
            facts["scope_id"],
            rule,
            facts,
            now,
            poll_seconds,
            classification,
            reason,
            evidence,
            expected,
            **kwargs,
        )

    if not facts.get("input_generation") or not timestamp(facts.get("received_at")):
        return [emit("unobservable", "admission_receipt_incomplete", evidence, expect)]
    disposition = facts.get("disposition")
    if disposition == "explicitly_ignored":
        return [
            emit(
                "healthy",
                "admission_explicitly_disposed",
                evidence,
                expect,
                positive={
                    "input_generation": facts["input_generation"],
                    "disposition": disposition,
                },
                progress_at=facts.get("disposition_at"),
            )
        ]
    if disposition in {
        "admitted_task",
        "queued_native_round",
        "linked_pending",
    } and facts.get("target_exists"):
        owned = emit(
            "healthy",
            "admission_owner_recorded",
            evidence,
            expect,
            positive={
                "input_generation": facts["input_generation"],
                "target_id": facts["target_id"],
            },
            progress_at=facts.get("disposition_at"),
        )
        operator = facts["scope_type"] == "work_item_event" and facts.get(
            "operator_handoff"
        )
        if disposition != "linked_pending" and not operator:
            return [owned]
        rule = "handoff_stalled"
        expect = {
            "kind": "admission_successor",
            "input_generation": facts["input_generation"],
        }
        if facts.get("successor_task_id"):
            return [
                owned,
                emit(
                    "healthy",
                    "admission_consumed",
                    evidence,
                    expect,
                    positive={
                        "successor_task_id": facts["successor_task_id"],
                        "input_generation": facts["input_generation"],
                    },
                    progress_at=facts.get("successor_occurred_at"),
                ),
            ]
        wait = facts.get("wait_reason")
        if wait or not (
            facts.get("pending_eligible") or facts.get("operator_handoff_eligible")
        ):
            return [
                owned,
                emit(
                    "expected_wait",
                    wait or "admission_linked_pending",
                    evidence,
                    expect,
                ),
            ]
        return [
            owned,
            _eligible(
                facts,
                rule,
                emit,
                now,
                stall_seconds,
                "admission_handoff_stalled",
                evidence,
                expect,
                [expect, facts.get("target_id")],
                consumer=facts.get("consumer_completed_at"),
                confirm=True,
            ),
        ]
    if facts.get("wait_reason"):
        return [emit("expected_wait", facts["wait_reason"], evidence, expect)]
    if disposition == "deferred":
        if not facts.get("consumer") or not timestamp(facts.get("due_at")):
            return [
                emit(
                    "unobservable",
                    "admission_deferred_condition_unknown",
                    evidence,
                    expect,
                )
            ]
        due = _future(now, facts["due_at"])
        if due:
            return [
                emit(
                    "expected_wait", "admission_deferred", evidence, expect, next_at=due
                )
            ]
    elif disposition is not None:
        return [emit("unobservable", "admission_target_unverified", evidence, expect)]
    return [
        _eligible(
            facts,
            rule,
            emit,
            now,
            stall_seconds,
            "admission_deferred_consumer_stalled"
            if disposition
            else "admission_disposition_missing",
            evidence,
            expect,
            [expect, facts.get("consumer"), facts.get("due_at")],
            consumer=facts.get("consumer_completed_at"),
            confirm=True,
        )
    ]


def evaluate_controller(facts, *, now, poll_seconds=60):
    now = timestamp(now)
    scope_id = facts["scope_id"]
    evidence = {
        "daemon_run_id": facts.get("daemon_run_id"),
        "event_id": facts.get("event_id"),
        "lease_id": facts.get("lease_id"),
    }
    expect = {"kind": "controller_progress", "scope_id": scope_id}
    status = facts.get("status")
    if status in {"disabled", "stopped"}:
        outcome = ("expected_wait", "controller_" + status)
    elif status == "attention":
        outcome = ("confirmed", "controller_lane_latched")
    elif status == "failed":
        outcome = ("confirmed", "controller_run_failed")
    elif status == "expired":
        overdue = _old_enough(now, facts.get("lease_expires_at"), poll_seconds)
        outcome = (
            ("confirmed", "controller_progress_stalled")
            if overdue
            else ("suspect", "controller_lease_expired")
        )
        evidence["lease_expires_at"] = facts.get("lease_expires_at")
    elif status in {"fresh", "completed"}:
        outcome = ("healthy", "controller_progress_observed")
    else:
        outcome = ("unobservable", "controller_progress_unverified")
    positive = (
        evidence
        if status == "completed" or (status == "fresh" and facts.get("has_progress"))
        else None
    )
    return _observation(
        "controller",
        scope_id,
        "controller_or_notice_stalled",
        facts,
        now,
        poll_seconds,
        *outcome,
        evidence,
        expect,
        positive=positive,
        progress_at=facts.get("completed_at") or facts.get("heartbeat_at"),
    )

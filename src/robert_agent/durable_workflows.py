"""Versioned DBOS workflow registrations for Robert subjects.

The workflow body is deliberately limited to deterministic branching over
validated step results.  Robert domain reads and all side effects belong in
the process-local driver invoked by named DBOS steps.
"""

from dbos import DBOS

from robert_agent import workflow_operations


def _attention_result(task_id, reason_code, state=None):
    return {
        "status": "attention",
        "reason_code": reason_code,
        "workflow": "human_request_workflow_v1",
        "subject_type": "task",
        "subject_id": task_id,
        "definition_version": "v1",
        "state": workflow_operations.validate_human_request_state(state or {}),
    }


def _terminal_result(task_id, transition):
    state = transition["state"]
    status = state["status"]
    return {
        "status": status,
        "workflow": "human_request_workflow_v1",
        "subject_type": "task",
        "subject_id": task_id,
        "definition_version": "v1",
        "state": state,
    }


def _terminal_order_reason(transition, completed_operations, publish_required):
    status = transition["state"]["status"]
    if status == "completed":
        if "finalize" not in completed_operations:
            return "terminal_out_of_order"
        if publish_required and "publish" not in completed_operations:
            return "terminal_out_of_order"
    elif status == "rejected":
        if "audit" not in completed_operations:
            return "terminal_out_of_order"
        if "publish" in completed_operations or "finalize" in completed_operations:
            return "terminal_out_of_order"
    return None


def _actionable_order_reason(operation, completed_operations, publish_required):
    """Return a fixed reason for a transition that cannot progress the task."""

    if operation in {"prepare", "dispatch", "audit", "publish", "finalize", "create_followup"}:
        if operation in completed_operations:
            return "transition_out_of_order"
    required_before = {
        "dispatch": "prepare",
        "supervise": "dispatch",
        "audit": "supervise",
        "publish": "audit",
        "finalize": "audit",
        "create_followup": "finalize",
    }.get(operation)
    if required_before and required_before not in completed_operations:
        return "transition_out_of_order"
    if operation == "finalize" and publish_required and "publish" not in completed_operations:
        return "transition_out_of_order"
    return None


@DBOS.step(name="human_request_next_transition_v1")
def human_request_next_transition_step(task_id: str) -> dict:
    driver = workflow_operations.current_human_request_driver()
    transition = driver.next_transition(task_id)
    return workflow_operations.validate_human_request_transition(transition)


@DBOS.step(name="human_request_apply_transition_v1")
def human_request_apply_transition_step(task_id: str, transition: dict) -> dict:
    driver = workflow_operations.current_human_request_driver()
    checked_transition = workflow_operations.validate_human_request_transition(transition)
    result = driver.apply_transition(task_id, checked_transition)
    return workflow_operations.validate_human_request_apply_result(checked_transition, result)


@DBOS.step(name="human_request_consume_signal_v1")
def human_request_consume_signal_step(task_id: str, message: dict) -> dict:
    driver = workflow_operations.current_human_request_driver()
    checked_message = workflow_operations.validate_human_request_signal(task_id, message)
    result = driver.consume_signal(task_id, checked_message)
    return workflow_operations.validate_human_request_consume_result(checked_message, result)


@DBOS.step(name="human_request_mark_attention_v1")
def human_request_mark_attention_step(task_id: str, reason_code: str) -> dict:
    if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 96:
        raise ValueError("human request attention reason must be a bounded string")
    driver = workflow_operations.current_human_request_driver()
    result = driver.mark_attention(task_id, reason_code)
    if result is None:
        state = {}
    elif isinstance(result, dict):
        state = result.get("state", {})
    else:
        raise ValueError("human request attention result has an invalid shape")
    return {
        "status": "attention",
        "reason_code": reason_code,
        "state": workflow_operations.validate_human_request_state(state),
    }


def _invalid_transition_attention(task_id, reason_code):
    human_request_mark_attention_step(task_id, reason_code)
    return _attention_result(task_id, reason_code)


@DBOS.workflow(name="human_request_workflow_v1")
def human_request_workflow_v1(task_id: str) -> dict:
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 256:
        raise ValueError("task_id must be a non-empty string")

    applied_transition_ids = set()
    completed_operations: set[str] = set()
    publish_required = False
    attention_called = False

    while True:
        try:
            transition = human_request_next_transition_step(task_id)
            transition = workflow_operations.validate_human_request_transition(transition)
        except ValueError:
            if not attention_called:
                attention_called = True
                return _invalid_transition_attention(task_id, "transition_invalid")
            return _attention_result(task_id, "transition_invalid")

        if transition["terminal"]:
            reason = _terminal_order_reason(
                transition,
                completed_operations,
                publish_required,
            )
            if reason:
                if not attention_called:
                    attention_called = True
                    return _invalid_transition_attention(task_id, reason)
                return _attention_result(task_id, reason)
            return _terminal_result(task_id, transition)

        operation = transition["operation"]
        if operation == "wait":
            message = DBOS.recv(
                "subject-events",
                timeout_seconds=transition["wait_seconds"],
            )
            if message is None:
                continue
            try:
                checked_message = workflow_operations.validate_human_request_signal(
                    task_id, message
                )
                consumed = human_request_consume_signal_step(task_id, checked_message)
                consumed = workflow_operations.validate_human_request_consume_result(
                    checked_message, consumed
                )
            except ValueError:
                if not attention_called:
                    attention_called = True
                    return _invalid_transition_attention(task_id, "signal_invalid")
                return _attention_result(task_id, "signal_invalid")
            if consumed["status"] == "attention":
                return _attention_result(task_id, "signal_attention", consumed["state"])
            continue

        if transition["transition_id"] in applied_transition_ids:
            if not attention_called:
                attention_called = True
                return _invalid_transition_attention(task_id, "transition_not_progressed")
            return _attention_result(task_id, "transition_not_progressed")

        if operation == "audit":
            publish_required = transition["state"]["action_count"] > 0

        reason = _actionable_order_reason(
            operation,
            completed_operations,
            publish_required,
        )
        if reason:
            if not attention_called:
                attention_called = True
                return _invalid_transition_attention(task_id, reason)
            return _attention_result(task_id, reason)

        try:
            applied = human_request_apply_transition_step(task_id, transition)
            applied = workflow_operations.validate_human_request_apply_result(
                transition, applied
            )
        except ValueError:
            if not attention_called:
                attention_called = True
                return _invalid_transition_attention(task_id, "apply_result_invalid")
            return _attention_result(task_id, "apply_result_invalid")

        applied_transition_ids.add(transition["transition_id"])
        completed_operations.add(operation)
        if applied["status"] == "attention":
            return _attention_result(task_id, "driver_attention", applied["state"])

        # The next loop iteration records the next durable transition.  No
        # clock, filesystem, SQLite, process, config, or external API is read
        # from this workflow body.


def _remediation_attention_result(episode_id, reason_code, state=None):
    return {
        "status": "attention",
        "reason_code": reason_code,
        "workflow": "pr_remediation_workflow_v1",
        "subject_type": "remediation_episode",
        "subject_id": episode_id,
        "definition_version": "v1",
        "state": workflow_operations.validate_remediation_state(state or {}),
    }


def _remediation_terminal_result(episode_id, transition):
    return {
        "status": transition["state"]["status"],
        "workflow": "pr_remediation_workflow_v1",
        "subject_type": "remediation_episode",
        "subject_id": episode_id,
        "definition_version": "v1",
        "state": transition["state"],
    }


def _remediation_terminal_order_reason(
    transition,
    completed_operations,
    fresh_signal_after_health,
):
    status = transition["state"]["status"]
    if status == "resolved":
        required = {"publish", "wait_health", "resolve"}
        if not required.issubset(completed_operations):
            return "terminal_out_of_order"
        if not fresh_signal_after_health:
            return "terminal_out_of_order"
    elif status == "exhausted" and "exhaust" not in completed_operations:
        return "terminal_out_of_order"
    elif status == "superseded" and "supersede" not in completed_operations:
        return "terminal_out_of_order"
    return None


def _remediation_order_reason(
    operation,
    completed_operations,
    *,
    snapshot_status,
    budget_ok,
    action_count,
    health_seen,
    fresh_signal_after_health,
):
    """Return a fixed reason for a transition that cannot safely progress."""

    if operation == "validate_snapshot":
        return (
            "transition_out_of_order"
            if "validate_snapshot" in completed_operations
            else None
        )
    if "validate_snapshot" not in completed_operations:
        return "transition_out_of_order"
    if operation == "enforce_budget":
        if snapshot_status != "current" or "enforce_budget" in completed_operations:
            return "transition_out_of_order"
        return None
    if operation in {"prepare", "dispatch", "supervise", "audit", "publish", "wait_health", "resolve"}:
        if snapshot_status != "current":
            return "transition_out_of_order"
    if operation in {"prepare", "dispatch"}:
        if "enforce_budget" not in completed_operations or budget_ok is not True:
            return "transition_out_of_order"
    if operation == "prepare":
        if "prepare" in completed_operations:
            return "transition_out_of_order"
    elif operation == "dispatch":
        if "prepare" not in completed_operations or "dispatch" in completed_operations:
            return "transition_out_of_order"
    elif operation == "supervise":
        if "dispatch" not in completed_operations or "supervise" in completed_operations:
            return "transition_out_of_order"
    elif operation == "audit":
        if "supervise" not in completed_operations or "audit" in completed_operations:
            return "transition_out_of_order"
    elif operation == "publish":
        if (
            "audit" not in completed_operations
            or "publish" in completed_operations
            or not isinstance(action_count, int)
            or action_count <= 0
        ):
            return "transition_out_of_order"
    elif operation == "wait_health":
        if "publish" not in completed_operations or "wait_health" in completed_operations:
            return "transition_out_of_order"
    elif operation == "resolve":
        if (
            "publish" not in completed_operations
            or "wait_health" not in completed_operations
            or "resolve" in completed_operations
            or not health_seen
            or not fresh_signal_after_health
        ):
            return "transition_out_of_order"
    elif operation == "wait":
        if "publish" not in completed_operations:
            return "transition_out_of_order"
    elif operation == "exhaust":
        if "exhaust" in completed_operations:
            return "transition_out_of_order"
        if (
            snapshot_status == "current"
            and budget_ok is not False
            and "audit" not in completed_operations
        ):
            return "transition_out_of_order"
    elif operation == "supersede":
        if "supersede" in completed_operations:
            return "transition_out_of_order"
    return None


@DBOS.step(name="pr_remediation_next_transition_v1")
def pr_remediation_next_transition_step(episode_id: str) -> dict:
    driver = workflow_operations.current_remediation_driver()
    transition = driver.next_transition(episode_id)
    return workflow_operations.validate_remediation_transition(transition)


@DBOS.step(name="pr_remediation_apply_transition_v1")
def pr_remediation_apply_transition_step(episode_id: str, transition: dict) -> dict:
    driver = workflow_operations.current_remediation_driver()
    checked_transition = workflow_operations.validate_remediation_transition(transition)
    result = driver.apply_transition(episode_id, checked_transition)
    return workflow_operations.validate_remediation_apply_result(checked_transition, result)


@DBOS.step(name="pr_remediation_consume_signal_v1")
def pr_remediation_consume_signal_step(episode_id: str, message: dict) -> dict:
    driver = workflow_operations.current_remediation_driver()
    checked_message = workflow_operations.validate_remediation_signal(episode_id, message)
    result = driver.consume_signal(episode_id, checked_message)
    return workflow_operations.validate_remediation_consume_result(checked_message, result)


@DBOS.step(name="pr_remediation_mark_attention_v1")
def pr_remediation_mark_attention_step(episode_id: str, reason_code: str) -> dict:
    if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 96:
        raise ValueError("remediation attention reason must be a bounded string")
    driver = workflow_operations.current_remediation_driver()
    result = driver.mark_attention(episode_id, reason_code)
    if result is None:
        state = {}
    elif isinstance(result, dict):
        state = result.get("state", {})
    else:
        raise ValueError("remediation attention result has an invalid shape")
    return {
        "status": "attention",
        "reason_code": reason_code,
        "state": workflow_operations.validate_remediation_state(state),
    }


def _remediation_invalid_attention(episode_id, reason_code):
    pr_remediation_mark_attention_step(episode_id, reason_code)
    return _remediation_attention_result(episode_id, reason_code)


@DBOS.workflow(name="pr_remediation_workflow_v1")
def pr_remediation_workflow_v1(episode_id: str) -> dict:
    if not isinstance(episode_id, str) or not episode_id.strip() or len(episode_id) > 256:
        raise ValueError("episode_id must be a non-empty string")

    applied_transition_ids = set()
    completed_operations: set[str] = set()
    snapshot_status = None
    budget_ok = None
    action_count = None
    health_seen = False
    fresh_signal_after_health = False
    attention_called = False

    while True:
        try:
            transition = pr_remediation_next_transition_step(episode_id)
            transition = workflow_operations.validate_remediation_transition(transition)
        except ValueError:
            if not attention_called:
                attention_called = True
                return _remediation_invalid_attention(episode_id, "transition_invalid")
            return _remediation_attention_result(episode_id, "transition_invalid")

        if transition["terminal"]:
            reason = _remediation_terminal_order_reason(
                transition,
                completed_operations,
                fresh_signal_after_health,
            )
            if reason:
                if not attention_called:
                    attention_called = True
                    return _remediation_invalid_attention(episode_id, reason)
                return _remediation_attention_result(episode_id, reason)
            return _remediation_terminal_result(episode_id, transition)

        operation = transition["operation"]
        if operation == "wait":
            reason = _remediation_order_reason(
                operation,
                completed_operations,
                snapshot_status=snapshot_status,
                budget_ok=budget_ok,
                action_count=action_count,
                health_seen=health_seen,
                fresh_signal_after_health=fresh_signal_after_health,
            )
            if reason:
                if not attention_called:
                    attention_called = True
                    return _remediation_invalid_attention(episode_id, reason)
                return _remediation_attention_result(episode_id, reason)
            message = DBOS.recv(
                "subject-events",
                timeout_seconds=transition["wait_seconds"],
            )
            if message is None:
                continue
            try:
                checked_message = workflow_operations.validate_remediation_signal(
                    episode_id, message
                )
                consumed = pr_remediation_consume_signal_step(
                    episode_id, checked_message
                )
                consumed = workflow_operations.validate_remediation_consume_result(
                    checked_message, consumed
                )
            except ValueError:
                if not attention_called:
                    attention_called = True
                    return _remediation_invalid_attention(episode_id, "signal_invalid")
                return _remediation_attention_result(episode_id, "signal_invalid")
            if consumed["status"] == "attention":
                return _remediation_attention_result(
                    episode_id,
                    "signal_attention",
                    consumed["state"],
                )
            if consumed["status"] == "consumed" and health_seen:
                fresh_signal_after_health = True
            continue

        if transition["transition_id"] in applied_transition_ids:
            if not attention_called:
                attention_called = True
                return _remediation_invalid_attention(
                    episode_id,
                    "transition_not_progressed",
                )
            return _remediation_attention_result(episode_id, "transition_not_progressed")

        reason = _remediation_order_reason(
            operation,
            completed_operations,
            snapshot_status=snapshot_status,
            budget_ok=budget_ok,
            action_count=action_count,
            health_seen=health_seen,
            fresh_signal_after_health=fresh_signal_after_health,
        )
        if reason:
            if not attention_called:
                attention_called = True
                return _remediation_invalid_attention(episode_id, reason)
            return _remediation_attention_result(episode_id, reason)

        try:
            applied = pr_remediation_apply_transition_step(episode_id, transition)
            applied = workflow_operations.validate_remediation_apply_result(
                transition, applied
            )
        except ValueError:
            if not attention_called:
                attention_called = True
                return _remediation_invalid_attention(episode_id, "apply_result_invalid")
            return _remediation_attention_result(episode_id, "apply_result_invalid")

        applied_transition_ids.add(transition["transition_id"])
        completed_operations.add(operation)
        if operation == "validate_snapshot":
            snapshot_status = transition["state"]["snapshot_status"]
        elif operation == "enforce_budget":
            budget_ok = transition["state"]["budget_ok"]
        elif operation == "audit":
            action_count = transition["state"]["action_count"]
        elif operation == "wait_health":
            health_seen = True
        if applied["status"] == "attention":
            return _remediation_attention_result(
                episode_id,
                "driver_attention",
                applied["state"],
            )

        # The next loop iteration records the next durable transition.  No
        # clock, filesystem, SQLite, process, config, or external API is read
        # from this workflow body.


@DBOS.step(name='robert.v2.advance_subject', retries_allowed=True, max_attempts=3, interval_seconds=1, backoff_rate=2)
def advance_subject_v2_step(subject_type, subject_id):
    from robert_agent.workflow_driver import current_driver
    return current_driver().advance(subject_type, subject_id)


def _run_subject_v2(subject_type, subject_id):
    while True:
        outcome = advance_subject_v2_step(subject_type, subject_id)
        if outcome['status'] != 'waiting':
            return outcome
        # Notifications are hints; the domain outbox/receipts remain authoritative.
        DBOS.recv('subject-events', timeout_seconds=5)


@DBOS.workflow(name='robert.human_request.v2')
def human_request_workflow_v2(task_id):
    return _run_subject_v2('task', task_id)


@DBOS.workflow(name='robert.pr_remediation.v2')
def pr_remediation_workflow_v2(episode_id):
    return _run_subject_v2('remediation_episode', episode_id)

"""Audit saved results with fresh execution and publication evidence."""

from robert_agent.persistence import transactions as db_transactions

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import attempts as db_read_attempts
from robert_agent.persistence.queries import work_items as db_read_work_items
from robert_agent.persistence.queries import worker_results as db_read_worker_results
from robert_agent.persistence.stores import attempts as db_write_attempts
from robert_agent.persistence.stores import tasks as db_write_tasks
from robert_agent.persistence.stores import worker_results as db_write_worker_results
from robert_agent.delivery.application import audit as audit_result
from robert_agent.delivery import results
from robert_agent.delivery.application import scope as result_scope
from robert_agent.execution.application import evidence
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import completion
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.context import project as project_memory
from robert_agent.delivery import contract as result_contract
from robert_agent.execution import notifications as execution_notifications
from robert_agent.execution import ownership as execution_ownership
from robert_agent.execution import retry as retry_policy
from robert_agent.foundation import commands as control_commands
from robert_agent.foundation import values
from robert_agent.readmodels import usage
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.work import wakeup
from robert_agent.workflows import pr_remediation as remediation
from robert_agent.workflows import routes as route
import json
import subprocess


def _record_result_usage_status(conn, attempt_id, result_id):
    attempt_row = db_records.first(db_read_attempts.find_metadata_json_by_attempt_id(conn, attempt_id))
    attempt_metadata = values.json_object(attempt_row[0] if attempt_row else None)
    usage_status = usage.extract_attempt_usage(attempt_metadata)
    if attempt_row and attempt_metadata.get("usage") != usage_status:
        attempt_metadata["usage"] = usage_status
        db_write_attempts.set_metadata_json_by_attempt_id(
            conn, json.dumps(attempt_metadata, ensure_ascii=False, sort_keys=True), attempt_id
        )
    result_row = db_records.first(db_read_worker_results.find_metadata_json_by_result_id(conn, result_id))
    result_metadata = values.json_object(result_row[0] if result_row else None)
    result_metadata["usage"] = usage_status
    db_write_worker_results.set_metadata_json_by_result_id(
        conn, json.dumps(result_metadata, ensure_ascii=False, sort_keys=True), result_id
    )
    return usage_status


def _revalidate_audit_claim(conn, claim, config, now):
    from robert_agent.supervision import actions as supervision_actions

    if not conn.in_transaction:
        db_transactions.begin(conn, immediate=True)
    if supervision_actions.current(conn, claim, config=config, now=now):
        return True
    # Git inspection is read-only. A stale claim has not applied an audit.
    supervision_actions.finish(
        conn, claim, outcome="rejected", now=now, receipt={"reason_code": "audit_target_changed_after_inspection"}
    )
    return False


def audit_completed_results(
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
    from robert_agent.supervision import actions as supervision_actions

    rows = db_read_worker_results.find_for_audit_completed_results(conn, repo_id)
    audited = 0
    for row in rows:
        result_row = row[:8]
        if not execution_ownership.claim_task(conn, row[1], run_now):
            continue
        if values.json_object(result_row[7]).get("audit"):
            continue
        workstream_id = row[8]
        allowed_actions = json.loads(row[9])
        expected_output = row[10]
        required_skills = json.loads(row[11] or "[]")
        recommended_skills = json.loads(row[12] or "[]")
        verification_policy = route.verification_policy_for(row[13])
        task_kind = row[14]
        task_metadata = values.json_object(row[15])
        if values.json_object(result_row[7]).get("result_contract") != result_contract.CONTRACT_ID:
            # Old results remain audit history or unresolved cutover work. Never
            # consume, publish or terminalize them through the new protocol.
            continue
        observation = values.json_object(result_row[7]).get("audit_observation", {})
        if not evidence.inspection_due(observation, run_now):
            continue
        payload = results.load_result_payload(conn, result_row)
        origin_row = db_records.first(db_read_work_items.find_origin_type_by_workstream_id(conn, workstream_id))
        origin_type = origin_row[0] if origin_row else "github"
        recovery_config = None if dry_run else config_result
        if not conn.in_transaction:
            db_transactions.begin(conn, immediate=True)
        gate = supervision_actions.begin(
            conn,
            task_id=payload["task_id"],
            kind="audit_retry",
            target_id=payload["result_id"],
            now=run_now,
            config=recovery_config,
            native_retry=bool(observation.get("attempt_count")),
        )
        if gate["status"] == "blocked":
            continue
        claim = gate.get("claim")
        _record_result_usage_status(conn, payload["attempt_id"], payload["result_id"])
        conn.commit()
        try:
            action_scope = result_scope.for_result(
                conn,
                payload["task_id"],
                payload["attempt_id"],
                workstream_id,
                command_runner=control_commands.run_command,
            )
        except ControlDeadlineExceeded:
            raise
        except subprocess.TimeoutExpired as exc:
            checked_at = values.utc_now() if claim else run_now
            if claim and not _revalidate_audit_claim(conn, claim, recovery_config, checked_at):
                continue
            failure = {
                **retry_policy.failure_state(observation, "Audit inspection timed out", run_now),
                "status": "inspection_timeout",
                "timeout_seconds": exc.timeout,
            }
            db_write_worker_results.update_for_audit_completed_results(conn, json.dumps(failure), payload["result_id"])
            if not failure["retryable"]:
                db_write_tasks.update_for_audit_completed_results(conn, payload["result_id"], payload["task_id"])
                item = work_items.resolve_work_item_for_workstream(conn, workstream_id)
                if item:
                    work_items.record_system_event(
                        conn,
                        item["work_item_id"],
                        event_type="execution_failed",
                        idempotency_key="audit:" + payload["result_id"],
                        body="Audit inspection stopped. Retry to inspect this saved result again.",
                        metadata={
                            "task_id": payload["task_id"],
                            "result_id": payload["result_id"],
                            "kind": "audit_inspection",
                        },
                        now=run_now,
                    )
                execution_notifications.insert(
                    conn, "audit_inspection_exhausted", "recorded", run_now, {"task_id": payload["task_id"], **failure}
                )
            if claim:
                supervision_actions.finish(
                    conn,
                    claim,
                    outcome="failed",
                    now=checked_at,
                    receipt={
                        "result_id": payload["result_id"],
                        "reason_code": "audit_inspection_timeout",
                        "native_retryable": failure["retryable"],
                    },
                )
            continue
        remediation_context = None
        remediation_attestation = None
        if task_kind in {"ci_remediation", "merge_conflict_remediation"}:
            episode = remediation.load_episode(
                conn,
                task_metadata.get("remediation_episode_id"),
            )
            if episode:
                remediation_context = {
                    "episode_id": episode["episode_id"],
                    "episode_kind": episode["episode_kind"],
                    "observed_head_sha": episode["observed_head_sha"],
                    "observed_base_sha": episode["observed_base_sha"],
                    "failure_signature": episode.get("failure_signature"),
                }
            if payload["output_type"] != "waiting_for_user":
                conn.commit()
                remediation_attestation = remediation.attest_remediation_worktree(
                    task_kind=task_kind,
                    action_scope=action_scope,
                    remediation_evidence=payload.get("remediation_evidence"),
                )
        checked_at = values.utc_now() if claim else run_now
        if claim and not _revalidate_audit_claim(conn, claim, recovery_config, checked_at):
            continue
        if not execution_ownership.claim_task(conn, payload["task_id"], run_now):
            continue
        audit = audit_result.audit_result(
            payload,
            allowed_actions,
            recommended_skills=recommended_skills,
            required_skills=required_skills,
            expected_output=expected_output,
            verification_policy=verification_policy,
            origin_type=origin_type,
            action_scope=action_scope,
            task_kind=task_kind,
            remediation_context=remediation_context,
            remediation_attestation=remediation_attestation,
            review_input_policy=task_metadata.get("review_input_policy", "required"),
        )
        if claim and not supervision_actions.finish(
            conn,
            claim,
            now=checked_at,
            outcome="observing" if audit["status"] == "accepted" else "failed",
            receipt={"result_id": payload["result_id"], "audit_status": audit["status"]},
        ):
            continue
        results.record_result_audit(conn, payload["result_id"], audit)
        wakeup.consume_wakeups_for_results(
            conn,
            result_ids=[payload["result_id"]],
            run_id=run_id,
            now=run_now,
        )
        if audit["status"] != "accepted":
            action_status = "policy_violation" if audit["status"] == "policy_violation" else "failed"
            results.set_github_actions_status(conn, payload["result_id"], action_status)
            work_application_lifecycle.finalize_failed_task(conn, payload["task_id"], workstream_id, run_now, audit)
            execution_notifications.insert(
                conn,
                "worker_result_rejected",
                "recorded",
                run_now,
                {
                    "task_id": payload["task_id"],
                    "result_id": payload["result_id"],
                    "audit": audit,
                },
            )
            audited += 1
            continue

        results.set_github_actions_status(conn, payload["result_id"], "accepted")
        memory_status = project_memory.record_memory_delta(
            conn,
            payload,
            workstream_id=workstream_id,
            repo_id=repo_id,
            run_now=run_now,
        )
        results.record_result_project_memory_status(conn, payload["result_id"], memory_status)
        if results.result_actions_all_published(conn, payload["result_id"]):
            try:
                completion.finalize_accepted_result(
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
        else:
            work_application_lifecycle.mark_task_waiting_for_publish(conn, payload["task_id"], workstream_id, run_now)
        audited += 1
    return audited

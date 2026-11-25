"""Persist scoped questions separately from a reviewed code publication."""

import json
import subprocess

from robert_agent.delivery import contract
from robert_agent.delivery.application import submission, validation
from robert_agent.execution import stage_evidence
from robert_agent.execution.stages import StageStopped, attempt_is_active
from robert_agent.foundation import question_choices
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.delivery import ReviewPublicationMode
from robert_agent.persistence import transactions
from robert_agent.persistence.queries import attempts
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.rounds.records import artifact_available
from robert_agent.security.redaction import redact_and_truncate
from robert_agent.workflows.review import workspace


def candidate_payload(coordinator, saved, *, source_path=None):
    """Legacy submissions keep their shape; new files retain their output type."""
    if isinstance(saved, dict) and "output_type" in saved and "submission" in saved:
        if set(saved) != {"task_id", "attempt_id", "output_type", "submission"}:
            raise ValueError("invalid candidate result envelope")
        kind, payload = saved["output_type"], saved["submission"]
        if kind not in {"new_pr", "waiting_for_user"} or not isinstance(payload, dict):
            raise ValueError("unsupported implementation output type")
        task_id, attempt_id = coordinator.task_id, coordinator.attempt_id
        if source_path is not None:
            with store.connect(coordinator.db_path) as conn:
                sources = [
                    run
                    for run in store.runs(conn, coordinator.round_id)
                    if run["stage"] == "implement"
                    and (
                        run["run_id"] == source_path.parent.name
                        or run["run_id"] == coordinator.handoff_source_run_id
                        or run["artifact"].get("candidate_path") == str(source_path)
                    )
                ]
                if len(sources) > 1:
                    raise StageStopped("candidate result owner is ambiguous")
                if sources:
                    attempt_id = sources[0]["owner_attempt_id"]
                    owners = attempts.find_task_id_by_attempt_id(conn, attempt_id)
                    if len(owners) != 1:
                        raise StageStopped("candidate result owner is missing")
                    task_id = owners[0][0]
        if saved["task_id"] != task_id or saved["attempt_id"] != attempt_id:
            raise StageStopped("candidate result owner does not match originating stage")
        return kind, payload
    return "new_pr", saved


def registered_source(coordinator, decision):
    digest = store.digest(decision.path)
    field = "candidate" if decision.stage == "implement" else "dispositions"
    with store.connect(coordinator.db_path) as conn:
        runs = store.runs(conn, coordinator.round_id)
    sources = [
        run
        for run in runs
        if run["stage"] == decision.stage
        and artifact_available(run)
        and run["artifact"].get(field + "_path") == str(decision.path)
        and run["artifact"].get(field + "_sha256") == digest
    ]
    if len(sources) != 1:
        raise StageStopped("question has no unique registered source")
    run = sources[0]
    if run["slot_reserved"] or not run["dispatch"].get("process_exited"):
        raise StageStopped("question source process has not exited")
    saved = run["dispatch"].get("result_checkpoint")
    if not saved:
        raise StageStopped("question source checkpoint missing")
    manifest = (
        coordinator.manifest
        if decision.stage != "implement"
        else {"implementing": True, **{key: saved.get(key) for key in ("snapshot_id", "head_sha", "base_sha")}}
    )
    if stage_evidence.observe_source(run, manifest, saved["worktree_path"]) != saved:
        raise StageStopped("question source worktree changed")
    if not coordinator.final_decision_feedback and run["artifact"].get("operator_question") != decision.question:
        raise StageStopped("question differs from registered source")
    return run, saved


def submit(coordinator, decision, report_sha):
    coordinator.check_active()
    validation.require_answerable_question(decision.question)
    run, source = registered_source(coordinator, decision)
    with store.connect(coordinator.db_path) as conn:
        entry = store.session(conn, task_id=coordinator.task_id)
        original_runs = store.runs(conn, coordinator.round_id)

    def check_evidence():
        if registered_source(coordinator, decision) != (run, source):
            raise StageStopped("question source checkpoint changed")
        if decision.stage != "implement":
            workspace.verify_snapshot(coordinator.manifest)
            candidate = coordinator.manifest["candidate"]
            if store.digest(candidate["candidate_path"]) != candidate["candidate_sha256"]:
                raise StageStopped("candidate handoff changed before question")
        if coordinator.original_request and (
            store.digest(coordinator.original_request["path"]) != coordinator.original_request["sha256"]
        ):
            raise StageStopped("question original request changed")
        if report_sha is not None and (
            entry["report_sha256"] != report_sha or store.digest(entry["report_path"]) != report_sha
        ):
            raise StageStopped("question report changed")
        if feedback := coordinator.final_decision_feedback:
            with store.connect(coordinator.db_path) as conn:
                current = store.session(conn, task_id=coordinator.task_id)
                current["feedback_runs"] = store.feedback_runs(
                    conn, coordinator.round_id, {feedback["source_run_id"], feedback["verification_run_id"]}
                )
            try:
                store.validate_final_feedback(current, feedback)
            except ValueError as exc:
                raise StageStopped(str(exc)) from exc

    check_evidence()
    try:
        observed_base = coordinator.observe_base(coordinator.repo["repo_root"], "upstream", coordinator.base)
    except (StageStopped, ControlDeadlineExceeded):
        raise
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Base observation is context; it does not grant code publication.
        observed_base = None
        base_error = redact_and_truncate(str(exc), 300).get("text", "base unavailable")
    else:
        base_error = None
    coordinator.check_active()
    check_evidence()
    field = "candidate" if decision.stage == "implement" else "dispositions"
    binding = {
        "stage": decision.stage,
        "round_id": coordinator.round_id,
        "snapshot_id": entry["snapshot_id"],
        "candidate_sha": source["worktree_head"] if decision.stage == "implement" else coordinator.manifest["head_sha"],
        "report_sha256": report_sha,
        "source_run_id": run["run_id"],
        field + "_path": str(decision.path),
        field + "_sha256": run["artifact"][field + "_sha256"],
        "source_checkpoint": source,
        "original_request": coordinator.original_request,
        "observed_base_sha": observed_base,
        **({"base_observation_error": base_error} if base_error else {}),
        **({"final_feedback": coordinator.final_decision_feedback} if coordinator.final_decision_feedback else {}),
    }
    if decision.stage == "implement":
        draft = candidate_payload(coordinator, json.loads(decision.path.read_text()), source_path=decision.path)[1]
    else:
        draft = coordinator.candidate_submission()
    question = decision.question
    comment = "\n\n".join(
        [question["summary"], "\n".join(question_choices.option_lines(question["choices"], recommended="（推荐）"))]
    ).strip()
    if (
        decision.stage == "address_findings"
        and coordinator.request["plan"].get("review_publication") == ReviewPublicationMode.PR_FIRST_V1
    ):
        if coordinator.origin_type == "github":
            owner = (coordinator.scope.get("decision_owner") or {}).get("login")
            if owner:
                comment = f"@{owner}\n\n{comment}"
        comment += "\n\n请在 Robert 工作台回复这条待决定事项，答复将绑定本轮原问题。"
    draft = {
        **draft,
        "planned_github_actions": [] if coordinator.origin_type == "web" else [{"type": "comment", "body": comment}],
        "operator_question": question,
        "handoff": "Waiting for an authenticated reply within the original task scope.",
    }
    coordinator.validate_submission(draft, output_type="waiting_for_user")
    envelope = {**coordinator.envelope(draft), "output_type": "waiting_for_user"}
    scope_keys = ("repo", "github_account", "base_branch", "worktree_path", "branch_name", "remote", "sources")
    proof = {
        "attempt_id": coordinator.attempt_id,
        "snapshot_id": entry["snapshot_id"],
        "report_sha256": report_sha,
        "head_sha": "",
        "decision": binding,
        "scope": {key: coordinator.scope[key] for key in scope_keys},
        "fingerprint": contract.semantic_fingerprint(envelope),
    }
    coordinator.check_active()
    check_evidence()
    with store.connect(coordinator.db_path) as conn:
        transactions.begin(conn, immediate=True)
        current = store.session(conn, task_id=coordinator.task_id)
        if not attempt_is_active(conn, coordinator.task_id, coordinator.attempt_id) or current != entry:
            raise StageStopped("question owner or input changed")
        current_runs = store.runs(conn, coordinator.round_id)
        if current_runs != original_runs or any(item["slot_reserved"] for item in current_runs):
            raise StageStopped("question source ownership changed")
        request = dict(current["request"])
        if decision.stage == "implement":
            request["implementation_question"] = binding
        store.update(
            conn,
            coordinator.round_id,
            status="waiting_for_user",
            proof_json=json.dumps(proof, sort_keys=True),
            request_json=json.dumps(request, sort_keys=True),
        )
    coordinator.request.update(request)
    result = submission.record_result(coordinator.db_path, envelope)
    if not result.get("ok"):
        raise StageStopped("question submission blocked: " + result.get("safe_error", result.get("status", "unknown")))
    return result


def verify_implementation_source(coordinator, binding):
    with store.connect(coordinator.db_path) as conn:
        entry = store.session(conn, task_id=coordinator.task_id)
    store.validate_reply_checkpoint(entry, binding)
    run = entry["question_run"]
    source = binding["source_checkpoint"]
    manifest = {"implementing": True, **{key: source.get(key) for key in ("snapshot_id", "head_sha", "base_sha")}}
    if stage_evidence.observe_source(run, manifest, source["worktree_path"]) != source:
        raise StageStopped("implementation question source changed before continuation")


def verify_address_source(coordinator, binding):
    with store.connect(coordinator.db_path) as conn:
        entry = store.session(conn, task_id=coordinator.task_id)
    store.validate_reply_checkpoint(entry, binding)
    run = entry["question_run"]
    checkpoint = binding["source_checkpoint"]
    if stage_evidence.observe_source(run, coordinator.manifest, checkpoint["worktree_path"]) != checkpoint:
        raise StageStopped("review question source changed before continuing the repair")

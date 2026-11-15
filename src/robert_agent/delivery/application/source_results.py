"""Accept fenced native root outcomes from audited, exited Worker candidates."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import uuid4

from robert_agent.delivery import native_contract
from robert_agent.delivery.application import source_delivery_observation, source_delivery_resume
from robert_agent.delivery.tapd_policy import PreparedComment, safe_mapped_text
from robert_agent.execution import ownership, source_model_output
from robert_agent.execution.application import source_models
from robert_agent.foundation.contracts.delivery import (
    NATIVE_RESULT_CONTRACT,
    AuditStatus,
    OutputType,
    PublicationStatus,
    PublishStatus,
    SourceControllerAction,
    SourceResultKind,
)
from robert_agent.foundation.contracts.execution import AttemptStatus, SourceModelStage
from robert_agent.foundation.contracts.work import (
    RequestEventState,
    SourceCapability,
    SourceRoundStatus,
    TaskKind,
    TaskLifecycle,
)
from robert_agent.foundation.tapd_identity import comment_name
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records, transactions
from robert_agent.persistence.queries import (
    repos as repo_queries,
)
from robert_agent.persistence.queries import (
    request_events,
    result_contract,
    source_github_publication,
    source_members,
    source_results,
    source_sessions,
    source_tasks,
)
from robert_agent.persistence.queries import (
    source_confirmation as confirmation_read,
)
from robert_agent.persistence.queries import (
    tapd_intake as intake_queries,
)
from robert_agent.persistence.rounds import source_feedback, source_notice
from robert_agent.persistence.rounds.source_evidence import context_digest, json_object
from robert_agent.persistence.stores import source_confirmation as confirmation_write
from robert_agent.persistence.stores import source_results as write
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.persistence.stores import tapd_intake as intake_write
from robert_agent.work import source_confirmation as confirmation_policy
from robert_agent.work import tapd_authority
from robert_agent.work.application import (
    source_confirmations,
    source_github_observation,
    source_observation,
    tapd_intake,
)

_ROOT_OUTPUTS = {
    OutputType.COMMENT_ANALYSIS: SourceResultKind.ANSWER,
    OutputType.WAITING_FOR_USER: SourceResultKind.QUESTION,
    OutputType.NEW_PR: SourceResultKind.ANSWER,
    OutputType.UPDATE_EXISTING_PR: SourceResultKind.ANSWER,
}
_DEVELOPMENT = {OutputType.NEW_PR, OutputType.UPDATE_EXISTING_PR}


def _configured_tapd(config, ticket):
    tapd = config.get("tapd", {})
    if (
        tapd.get("enabled") is True
        and tapd.get("host") == ticket.host
        and sum(project["workspace_id"] == ticket.workspace_id for project in tapd.get("projects", ())) == 1
    ):
        return tapd
    return None


def _source_scope(config, ticket, claim):
    return _configured_tapd(config, ticket) if ticket.source_id == claim.source_id else None


def _accepted_candidate(conn, result_id, round_, ticket):
    candidate = source_results.worker_candidate(conn, result_id)
    if (
        candidate is None
        or candidate["output_type"] not in _ROOT_OUTPUTS
        or candidate["task_kind"] != TaskKind.HUMAN_REQUEST
        or candidate["lifecycle"] != TaskLifecycle.RUNNING
        or candidate["attempt_status"] != AttemptStatus.COMPLETED
    ):
        return None
    task_id = candidate["task_id"]
    binding = result_contract.task_round_binding(conn, task_id)
    if (
        binding is None
        or binding["source_id"] != ticket.source_id
        or binding["round_id"] != round_["round_id"]
        or not source_sessions.attempt_is_settled(
            conn, task_id, candidate["attempt_id"], subject_type="task", subject_id=task_id, require_exit=True
        )
    ):
        return None
    metadata = json_object(candidate["metadata_json"]) or {}
    audit = metadata.get("audit")
    normalized = metadata.get("normalized_payload")
    source = normalized.get("native_source") if isinstance(normalized, dict) else None
    role = source.get("role") if isinstance(source, dict) else None
    target = {
        "provider": "tapd",
        "host": ticket.host,
        "workspace_id": ticket.workspace_id,
        "kind": ticket.kind,
        "external_id": ticket.external_id,
    }
    output = candidate["output_type"]
    fingerprint = metadata.get("submission_fingerprint")
    if (
        metadata.get("result_contract") != NATIVE_RESULT_CONTRACT
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or not isinstance(audit, dict)
        or audit.get("status") != AuditStatus.ACCEPTED
        or audit.get("ok") is not True
        or role not in {"answer", "development"}
        or (output in _DEVELOPMENT and (role != "development" or binding["capability"] != "development"))
        or source
        != {
            **target,
            **{key: binding[key] for key in ("source_id", "round_id", "request_id", "repo_id", "capability")},
            "round_input_sha256": binding["input_sha256"],
            "role": role,
        }
    ):
        return None
    return candidate, binding, audit, target, fingerprint


def github_candidate_target(binding, output):
    """Derive the original controller target from the durable Worker binding."""
    target = {
        "provider": "github",
        "host": "github.com",
        "repository": binding["full_name"],
        "repo_id": binding["repo_id"],
        "kind": "repository",
    }
    if output == OutputType.UPDATE_EXISTING_PR:
        target.update(kind="pull_request", external_id=binding["primary_external_id"])
    return target


def _audited_actions(conn, candidate, binding, audit, ticket_target):
    output = candidate["output_type"]
    actions = source_results.candidate_actions(conn, candidate["result_id"])
    selectors = native_contract.selectors_for(output)
    if len(actions) != len(selectors) or audit.get("planned_actions") != selectors:
        return ()
    github_target = github_candidate_target(binding, output)
    for row, selector in zip(actions, selectors, strict=True):
        metadata = json_object(row["metadata_json"]) or {}
        target = ticket_target if selector["provider"] == "tapd" else github_target
        if (
            row["task_id"] != candidate["task_id"]
            or row["source_result_id"] is not None
            or row["audit_status"] != AuditStatus.ACCEPTED
            or row["publish_status"] != PublishStatus.NOT_PUBLISHED
            or row["publication_id"] is not None
            or row["external_id"] is not None
            or row["provider"] != selector["provider"]
            or row["action_type"] != selector["type"]
            or metadata.get("target_id") != selector["target_id"]
            or json_object(row["target_json"]) != target
        ):
            return ()
    return tuple(row["action_id"] for row in actions)


def _handled_members(conn, round_id, members, encoded):
    try:
        fingerprints = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        return ()
    if (
        not isinstance(fingerprints, list)
        or not fingerprints
        or any(not isinstance(value, str) for value in fingerprints)
        or len(set(fingerprints)) != len(fingerprints)
    ):
        return ()
    groups = defaultdict(list)
    all_fingerprints = set()
    for row in request_events.source_round_events(conn, round_id):
        fingerprint = row["event_fingerprint"]
        if (
            row["state"] != RequestEventState.PENDING
            or row["event_id"] != row["frozen_event_id"]
            or fingerprint in all_fingerprints
        ):
            return ()
        all_fingerprints.add(fingerprint)
        groups[row["request_id"]].append(row)
    wanted = set(fingerprints)
    if set(groups) != {member["request_id"] for member in members} or not wanted <= all_fingerprints:
        return ()
    handled = []
    for request_id, rows in groups.items():
        associated = {row["event_fingerprint"] for row in rows}
        if wanted.intersection(associated):
            if not associated <= wanted:
                return ()
            handled.append(
                {
                    "request_id": request_id,
                    "content_version": rows[0]["content_version"],
                    "events": [
                        {
                            "row_id": row["row_id"],
                            "event_id": row["event_id"],
                            "fingerprint": row["event_fingerprint"],
                        }
                        for row in rows
                    ],
                }
            )
    return tuple(handled)


def _current_repo(conn, config, binding):
    repo = source_tasks.repository(conn, binding["repo_id"])
    matches = [item for item in config.get("repos", ()) if item["full_name"] == binding["full_name"]]
    return (
        repo is not None
        and repo["full_name"] == binding["full_name"]
        and len(matches) == 1
        and matches[0].get("enabled", True) is True
        and all(
            matches[0].get(field) == repo[field]
            for field in ("repo_root", "worktree_root", "default_base_branch", "github_account")
        )
    )


def _mixed_grants(conn, config, ticket, claim, round_, *, revision, now, mixed_proof):
    if not source_members.round_github_request_ids(conn, round_["round_id"]):
        return {} if mixed_proof is None else None
    return tapd_intake.current_mixed_worker_grants(
        conn, config, ticket, claim, round_, revision=revision, proof=mixed_proof, now=now
    )


def _current_grants(conn, tapd, ticket, members, revision, binding, github):
    for member in members:
        request_id = member["request_id"]
        decision = github.get(request_id) or tapd_intake.revalidate_request(
            conn, tapd, ticket, request_id, revision=revision
        )
        if (
            decision.reason != "allowed"
            or binding["full_name"] not in decision.repos
            or decision.capability != binding["round_capability"]
        ):
            return False
    return True


def _feedback_evidence(conn, round_id, handled, github, candidate=None):
    if not github:
        return {}
    targets = source_members.handled_return_targets(conn, round_id, handled)
    if targets is None:
        return None
    evidence = {"github_return_targets": targets}
    if targets and candidate is not None:
        try:
            code = source_feedback.code_digest(candidate)
        except ValueError:
            return None
        if code is not None:
            evidence["github_feedback_code_sha256"] = code
    return evidence


def _identity(key):
    return "source-result:" + sha256(key.encode()).hexdigest()


def accept_worker_result(conn, config, ticket, claim, *, round_id, worker_result_id, revision, now, mixed_proof=None):
    """Consume a verified subset of frozen members and bind its exact audited actions atomically."""
    tapd = _source_scope(config, ticket, claim)
    if tapd is None or not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return None
    existing = source_results.for_round(conn, round_id)
    if existing is not None:
        if existing["source_id"] == ticket.source_id and existing["worker_result_id"] == worker_result_id:
            return existing["source_result_id"]
        return None
    round_ = fence.lock_round(conn, claim, round_id, now=now)
    if round_ is None:
        return None
    accepted = _accepted_candidate(conn, worker_result_id, round_, ticket)
    if accepted is None:
        return None
    candidate, binding, audit, target, fingerprint = accepted
    if not _current_repo(conn, config, binding) or binding["repo_id"] not in json.loads(round_["repo_ids_json"]):
        return None
    members = fence.pending_round_members(conn, round_id)
    github = _mixed_grants(conn, config, ticket, claim, round_, revision=revision, now=now, mixed_proof=mixed_proof)
    if not members or github is None or not _current_grants(conn, tapd, ticket, members, revision, binding, github):
        return None
    handled = _handled_members(conn, round_id, members, candidate["consumed_event_fingerprints_json"])
    action_ids = _audited_actions(conn, candidate, binding, audit, target)
    feedback = _feedback_evidence(conn, round_id, handled, github, candidate)
    if (
        not handled
        or feedback is None
        or not action_ids
        or source_sessions.blockers_for_worker_result(conn, ticket.source_id, candidate["task_id"], action_ids)
    ):
        return None
    key = "tapd-round:" + round_id
    result_id = _identity(key)
    checkpoint = "accept_tapd_worker_root"
    transactions.savepoint(conn, checkpoint)
    try:
        write.insert_result(
            conn,
            source_result_id=result_id,
            source_id=ticket.source_id,
            round_id=round_id,
            kind=_ROOT_OUTPUTS[candidate["output_type"]],
            input_version=round_["input_version"],
            idempotency_key=key,
            worker_result_id=worker_result_id,
            evidence={
                "attempt_id": candidate["attempt_id"],
                "task_id": candidate["task_id"],
                "repo_id": binding["repo_id"],
                "round_input_sha256": round_["input_sha256"],
                "submission_fingerprint": fingerprint,
                "observation_revision": revision,
                "handled_members": handled,
                **feedback,
            },
            payload={"output_type": candidate["output_type"]},
            now=now,
        )
        action_ids += write.insert_source_feedback(conn, source_result_id=result_id, now=now) if github else ()
        write.attach_required_actions(conn, source_result_id=result_id, action_ids=action_ids)
        write.consume_accepted_members(
            conn,
            source_result_id=result_id,
            row_ids=tuple(event["row_id"] for member in handled for event in member["events"]),
            now=now,
        )
        if not write.finish_worker_execution(conn, task_id=candidate["task_id"], now=now):
            raise ValueError("Accepted Worker task changed during root acceptance")
        ownership.close_terminal_task(conn, candidate["task_id"], now)
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return result_id


def _model_candidate(conn, round_, run_id):
    if round_["capability"] != SourceCapability.READONLY:
        return None
    verified = source_models.verified_model_output(conn, round_, run_id)
    if verified is None:
        return None
    run, context, output, supplied = verified
    if output["kind"] not in {"answer", "question"}:
        return None
    children = context["child_results"]
    if run["stage"] == SourceModelStage.SYNTHESIS:
        if not source_models.synthesis_evidence_matches(conn, round_, children, supplied):
            return None
        if output["kind"] == "answer" and any("investigation_question" in child["content"] for child in children):
            return None
        repo_ids = tuple(sorted(child["repo_id"] for child in children))
    elif run["stage"] == SourceModelStage.IDENTIFY and output["kind"] == "question":
        if children or source_sessions.round_tasks(conn, round_["round_id"]):
            return None
        repo_ids = tuple(context["candidates"])
    else:
        return None
    body = output[output["kind"]]
    refs = output.get("references", [])
    if not safe_mapped_text(body, {ref["path"] for ref in refs}):
        return None
    handled = source_model_output.handled_members(output["handled_event_fingerprints"], context["request_members"])
    evidence = {
        "stage": run["stage"],
        "prompt_sha256": run["prompt_sha256"],
        "output_sha256": context_digest(output),
        "round_input_sha256": round_["input_sha256"],
        "handled_members": handled,
        "repo_ids": list(repo_ids),
    }
    payload = {"kind": output["kind"], "body": body, "references": refs}
    return evidence, payload


def _model_repo_names(conn, config, repo_ids):
    names = []
    for repo_id in repo_ids:
        row = source_tasks.repository(conn, repo_id)
        if row is None or not _current_repo(conn, config, row):
            return None
        names.append(row["full_name"])
    return frozenset(names)


def _model_grant_matches(decision, repo_names):
    return (
        decision.reason == "allowed"
        and decision.capability == SourceCapability.READONLY
        and repo_names <= set(decision.repos)
    )


def accept_model_result(conn, config, ticket, claim, *, round_id, model_run_id, revision, now, mixed_proof=None):
    """Accept one durable question or synthesis answer and create its required comment."""
    tapd = _source_scope(config, ticket, claim)
    if tapd is None or not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return None
    existing = source_results.for_round(conn, round_id)
    if existing is not None:
        return (
            existing["source_result_id"]
            if (existing["source_id"] == ticket.source_id and existing["model_run_id"] == model_run_id)
            else None
        )
    round_ = fence.lock_round(conn, claim, round_id, now=now)
    source = source_results.tapd_source(conn, ticket.source_id)
    if (
        round_ is None
        or source is None
        or (source["host"], source["project_key"], source["source_type"], source["external_id"]) != ticket.key
        or json_object(round_["publication_scope_json"])
        != {**source_results.source_target(source), "action": "comment"}
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
        or source_sessions.blockers_for_source(conn, ticket.source_id, limit=1)
    ):
        return None
    candidate = _model_candidate(conn, round_, model_run_id)
    if candidate is None:
        return None
    evidence, payload = candidate
    repo_names = _model_repo_names(conn, config, evidence["repo_ids"])
    members = fence.pending_round_members(conn, round_id)
    github = _mixed_grants(conn, config, ticket, claim, round_, revision=revision, now=now, mixed_proof=mixed_proof)
    if (
        repo_names is None
        or not members
        or github is None
        or any(
            not _model_grant_matches(
                github.get(row["request_id"])
                or tapd_intake.revalidate_request(conn, tapd, ticket, row["request_id"], revision=revision),
                repo_names,
            )
            for row in members
        )
    ):
        return None
    feedback = _feedback_evidence(conn, round_id, evidence["handled_members"], github)
    if feedback is None:
        return None
    key = "tapd-round:" + round_id
    result_id = _identity(key)
    action_id = "action-" + sha256(result_id.encode()).hexdigest()
    checkpoint = "accept_tapd_model_root"
    transactions.savepoint(conn, checkpoint)
    try:
        write.insert_result(
            conn,
            source_result_id=result_id,
            source_id=ticket.source_id,
            round_id=round_id,
            kind=payload["kind"],
            input_version=round_["input_version"],
            idempotency_key=key,
            model_run_id=model_run_id,
            evidence={**evidence, "observation_revision": revision, **feedback},
            payload=payload,
            now=now,
        )
        write.insert_source_comment(
            conn,
            source_result_id=result_id,
            action_id=action_id,
            target=source_results.source_target(source),
            metadata={"target_id": "ticket", "body": payload["body"]},
            now=now,
        )
        feedback_ids = write.insert_source_feedback(conn, source_result_id=result_id, now=now) if github else ()
        write.attach_required_actions(conn, source_result_id=result_id, action_ids=(action_id, *feedback_ids))
        write.consume_accepted_members(
            conn,
            source_result_id=result_id,
            row_ids=tuple(event["row_id"] for member in evidence["handled_members"] for event in member["events"]),
            now=now,
        )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return result_id


def _confirmation_repo_current(conn, config, scope):
    name = scope["current_rule"]["repo"]
    repos = [repo for repo in config.get("repos", ()) if repo["full_name"] == name]
    row = records.first(repo_queries.find_repo_id_by_full_name(conn, name))
    stored = source_tasks.repository(conn, row[0]) if row is not None else None
    return (
        len(repos) == 1
        and repos[0].get("enabled", True) is True
        and stored is not None
        and stored["full_name"] == name
        and all(
            repos[0].get(field) == stored[field]
            for field in (
                "repo_root",
                "worktree_root",
                "default_base_branch",
                "github_account",
            )
        )
    )


def accept_confirmation_question(conn, config, ticket, claim, *, revision, now):
    """Persist a versioned option question for one settled, paused rule binding."""
    tapd = _source_scope(config, ticket, claim)
    if tapd is None or not revision or not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return None
    offered = tapd_intake.paused_confirmation_scope(conn, tapd, ticket, revision=revision)
    if offered is None or not _confirmation_repo_current(conn, config, offered):
        return None
    current = confirmation_read.current(conn, ticket.source_id)
    if current is not None:
        return current["source_result_id"] if source_confirmations.scope_for_question(current) == offered else None
    if (
        source_sessions.active_round(conn, ticket.source_id)
        or source_sessions.blockers_for_source(conn, ticket.source_id, limit=1)
        or source_results.unknown_github_effect(conn, ticket.source_id, require_positive_receipt=True)
    ):
        return None
    source = source_results.tapd_source(conn, ticket.source_id)
    if source is None:
        return None
    version = confirmation_read.next_version(conn, ticket.source_id)
    question_id = "q-" + uuid4().hex
    key = f"tapd-confirmation:{ticket.source_id}:{version}"
    result_id = _identity(key)
    action_id = "action-" + sha256(result_id.encode()).hexdigest()
    digest = tapd_authority.digest(offered)
    checkpoint = "accept_source_confirmation"
    transactions.savepoint(conn, checkpoint)
    try:
        write.insert_result(
            conn,
            source_result_id=result_id,
            source_id=ticket.source_id,
            round_id=None,
            kind=SourceResultKind.CONTROL_NOTICE,
            input_version=revision,
            idempotency_key=key,
            evidence={
                "confirmation_question_id": question_id,
                "confirmation_version": version,
                "confirmation_scope_digest": digest,
            },
            payload={"reason": "confirmation_required"},
            now=now,
        )
        write.insert_source_comment(
            conn,
            source_result_id=result_id,
            action_id=action_id,
            target=source_results.source_target(source),
            metadata={"target_id": "ticket", "body": confirmation_policy.question_body(question_id, version, offered)},
            now=now,
        )
        write.attach_required_actions(conn, source_result_id=result_id, action_ids=(action_id,))
        confirmation_write.insert_question(
            conn,
            question_id=question_id,
            source_id=ticket.source_id,
            version=version,
            scope=offered,
            scope_digest=digest,
            source_result_id=result_id,
            action_id=action_id,
            now=now,
        )
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return result_id


def _confirmation_notice_current(conn, config, ticket, root, action, revision):
    evidence = json_object(root["evidence_json"]) or {}
    current = confirmation_read.current(conn, ticket.source_id)
    if (
        current is None
        or current["source_result_id"] != root["source_result_id"]
        or current["action_id"] != action["action_id"]
    ):
        return False
    scope = source_confirmations.scope_for_question(current)
    metadata = json_object(action["metadata_json"]) or {}
    return (
        scope is not None
        and evidence
        == {
            "confirmation_question_id": current["question_id"],
            "confirmation_version": current["version"],
            "confirmation_scope_digest": current["scope_digest"],
        }
        and tapd_intake.paused_confirmation_scope(conn, config["tapd"], ticket, revision=revision) == scope
        and _confirmation_repo_current(conn, config, scope)
        and {key: val for key, val in metadata.items() if key != "publish"}
        == {
            "target_id": "ticket",
            "body": confirmation_policy.question_body(current["question_id"], current["version"], scope),
        }
        and source_sessions.active_round(conn, ticket.source_id) is None
        and not source_results.unknown_github_effect(conn, ticket.source_id, require_positive_receipt=True)
    )


def _ordinary_notice_identity(ticket, episode):
    return source_notice.identity(ticket.source_id, episode["repositories"], episode["generation"])


def retire_obsolete_control_notices(conn, config, ticket, claim, *, revision, now):
    """Locally retire only proven obsolete, never-started fixed source comments."""
    tapd = _source_scope(config, ticket, claim)
    if (
        tapd is None
        or not conn.in_transaction
        or not fence.lock_claim(conn, claim, now=now)
        or source_sessions.active_round(conn, ticket.source_id)
    ):
        return ()
    episode = tapd_intake.fixed_notice_episode(conn, tapd, ticket, revision=revision)
    if episode is None:
        return ()
    current_key = _ordinary_notice_identity(ticket, episode)[0] if episode["repositories"] else None
    retired = []
    for result_id in source_results.pending_fixed_notices(conn, ticket.source_id):
        root = source_results.for_source(conn, ticket.source_id, result_id)
        if root["idempotency_key"] != current_key and write.retire_fixed_notice(
            conn, source_id=ticket.source_id, source_result_id=result_id, observation_revision=revision, now=now
        ):
            retired.append(result_id)
    return tuple(retired)


def accept_control_notice(conn, config, ticket, claim, *, revision, now):
    """Create an ordinary comment candidate for a current fixed pre-admission conflict."""
    tapd = _source_scope(config, ticket, claim)
    if tapd is None or not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return None
    episode = tapd_intake.fixed_notice_episode(conn, tapd, ticket, revision=revision)
    if episode is None or not episode["repositories"]:
        return None
    source = source_results.tapd_source(conn, ticket.source_id)
    if source is None:
        return None
    key, evidence = _ordinary_notice_identity(ticket, episode)
    existing = source_results.by_idempotency_key(conn, key)
    if existing is not None:
        return existing["source_result_id"] if source_results.fixed_notice_action(conn, source, existing) else None
    if source_sessions.active_round(conn, ticket.source_id) or source_sessions.blockers_for_source(
        conn, ticket.source_id
    ):
        return None
    result_id = _identity(key)
    action_id = source_notice.action_id(result_id)
    checkpoint = "accept_tapd_control_notice"
    transactions.savepoint(conn, checkpoint)
    try:
        write.insert_result(
            conn,
            source_result_id=result_id,
            source_id=ticket.source_id,
            round_id=None,
            kind=SourceResultKind.CONTROL_NOTICE,
            input_version=revision,
            idempotency_key=key,
            evidence=evidence,
            payload={"reason": source_notice.REASON},
            now=now,
        )
        write.insert_source_comment(
            conn,
            source_result_id=result_id,
            action_id=action_id,
            target=source_results.source_target(source),
            metadata={"target_id": "ticket", "body": source_notice.BODY},
            now=now,
        )
        write.attach_required_actions(conn, source_result_id=result_id, action_ids=(action_id,))
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.release(conn, checkpoint)
    return result_id


@dataclass(frozen=True, slots=True)
class CurrentSourceDeliveryGrant:
    """Current source authorization under the caller's writer; effect checks are separate."""

    source_result_id: str
    action_id: str
    revision: str
    request_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TapdReconciliationScope:
    """Frozen read scope for an already-uncertain comment."""

    publication_id: str
    target: tuple[str, str, str, str]
    publisher_principal: str
    prepared: PreparedComment = field(repr=False)


def _required_effect(conn, ticket, source_result_id, action_id):
    root = source_results.for_source(conn, ticket.source_id, source_result_id)
    source = source_results.tapd_source(conn, ticket.source_id) if root else None
    if (
        source is None
        or (source["host"], source["project_key"], source["source_type"], source["external_id"]) != ticket.key
    ):
        return None
    actions = source_results.required_actions(conn, source_result_id)
    if source_results.required_action_sets(conn, root, actions) is None:
        return None
    action = next((row for row in actions if row["action_id"] == action_id), None)
    if action is None or not required_effect(root, action, source_results.source_target(source)):
        return None
    return root, action


def required_effect(root, action, target):
    """Validate the owner and scope of an action read from the ordered required set."""
    if action["audit_status"] != AuditStatus.ACCEPTED or action["provider"] not in {"tapd", "github"}:
        return False
    evidence = json_object(root["evidence_json"]) or {}
    if action["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
        planned = source_feedback.planned_actions(
            root["source_result_id"], evidence.get("github_return_targets"), evidence.get("github_feedback_code_sha256")
        )
        return bool(planned and any(source_feedback.action_matches(root, action, item) for item in planned))
    if root["worker_result_id"] is not None:
        if (
            action["source_result_id"] is not None
            or action["result_id"] != root["worker_result_id"]
            or action["task_id"] != evidence.get("task_id")
        ):
            return False
    elif (
        action["source_result_id"] != root["source_result_id"]
        or action["task_id"] is not None
        or action["result_id"] is not None
    ):
        return False
    return action["provider"] != "tapd" or (
        action["action_type"] == "comment" and json_object(action["target_json"]) == target
    )


def _unstarted_effect(action):
    return action["publish_status"] == PublishStatus.NOT_PUBLISHED and (
        action["publication_id"] is None
        or (
            action["intent_status"] == PublicationStatus.PREPARED
            and action["intent_started_at"] is None
            and action["intent_finished_at"] is None
        )
    )


def _notice_current(conn, tapd, ticket, root, action, revision):
    episode = tapd_intake.fixed_notice_episode(conn, tapd, ticket, revision=revision)
    if episode is None or not episode["repositories"] or source_sessions.active_round(conn, ticket.source_id):
        return False
    current_key, evidence = _ordinary_notice_identity(ticket, episode)
    source = source_results.tapd_source(conn, ticket.source_id)
    if source is None:
        return False
    candidate = source_results.fixed_notice_action(conn, source, root)
    return (
        candidate is not None
        and candidate["action_id"] == action["action_id"]
        and source_notice.never_started(candidate)
        and root["idempotency_key"] == current_key
        and json_object(root["evidence_json"]) == evidence
    )


def _consumed_worker_lineage(conn, root, round_, evidence):
    """Bind the root to the same exited Worker and frozen repository assignment."""
    if root["worker_result_id"] is None:
        return None
    candidate = source_results.worker_candidate(conn, root["worker_result_id"])
    task_id = evidence.get("task_id")
    binding = result_contract.task_round_binding(conn, task_id)
    if (
        candidate is None
        or binding is None
        or candidate["task_id"] != task_id
        or candidate["attempt_id"] != evidence.get("attempt_id")
        or candidate["attempt_status"] != AttemptStatus.COMPLETED
        or candidate["lifecycle"] != TaskLifecycle.COMPLETED
        or binding["round_id"] != root["round_id"]
        or binding["source_id"] != root["source_id"]
        or binding["repo_id"] != evidence.get("repo_id")
        or binding["repo_id"] not in json.loads(round_["repo_ids_json"])
    ):
        return None
    metadata = json_object(candidate["metadata_json"]) or {}
    audit = metadata.get("audit")
    if (
        metadata.get("result_contract") != NATIVE_RESULT_CONTRACT
        or not evidence.get("submission_fingerprint")
        or metadata.get("submission_fingerprint") != evidence["submission_fingerprint"]
        or not isinstance(audit, dict)
        or audit.get("status") != AuditStatus.ACCEPTED
    ):
        return None
    return binding


def accepted_delivery_lineage(conn, config, root, round_, action):
    """Reprove an accepted candidate and its configured repository for one required action."""
    evidence = json_object(root["evidence_json"]) or {}
    if action["action_type"] == SourceControllerAction.GITHUB_FEEDBACK:
        plan = source_results.feedback_plan(conn, root)
        match = next((item for item in plan or () if source_feedback.action_matches(root, action, item)), None)
        if match is None or not _current_repo(
            conn, config, {"repo_id": match.target["repo_id"], "full_name": match.target["repository"]}
        ):
            return None, None
        if root["model_run_id"] is not None:
            candidate = _model_candidate(conn, round_, root["model_run_id"])
            if candidate is None:
                return None, None
            proof, payload = candidate
            if (
                any(evidence.get(key) != value for key, value in proof.items())
                or json_object(root["payload_json"]) != payload
                or match.target["repo_id"] not in proof["repo_ids"]
            ):
                return None, None
        else:
            binding = _consumed_worker_lineage(conn, root, round_, evidence)
            if binding is None or binding["repo_id"] != match.target["repo_id"]:
                return None, None
        return frozenset({match.target["repository"]}), match.target["repo_id"]
    if root["model_run_id"] is not None:
        candidate = _model_candidate(conn, round_, root["model_run_id"])
        if candidate is None or action["source_result_id"] != root["source_result_id"] or action["provider"] != "tapd":
            return None, None
        proof, payload = candidate
        metadata = json_object(action["metadata_json"]) or {}
        if (
            any(evidence.get(key) != value for key, value in proof.items())
            or json_object(root["payload_json"]) != payload
            or root["kind"] != payload["kind"]
            or metadata.get("target_id") != "ticket"
            or metadata.get("body") != payload["body"]
        ):
            return None, None
        return _model_repo_names(conn, config, proof["repo_ids"]), None
    binding = _consumed_worker_lineage(conn, root, round_, evidence)
    if binding is None or not _current_repo(conn, config, binding) or action["task_id"] != evidence["task_id"]:
        return None, None
    if action["provider"] == "github":
        candidate = source_results.worker_candidate(conn, root["worker_result_id"])
        metadata = json_object(candidate["metadata_json"]) or {}
        accepted_scope = metadata["audit"].get("action_scope")
        repository = source_tasks.repository(conn, binding["repo_id"])
        if (
            not isinstance(accepted_scope, dict)
            or not accepted_scope.get("github_account")
            or accepted_scope["github_account"] != repository["github_account"]
        ):
            return None, None
        target = json_object(action["target_json"]) or {}
        if binding["capability"] != "development" or any(
            target.get(key) != value
            for key, value in {
                "provider": "github",
                "host": "github.com",
                "repo_id": binding["repo_id"],
                "repository": binding["full_name"],
            }.items()
        ):
            return None, None
    return frozenset({binding["full_name"]}), binding["repo_id"]


def authorize_delivery(
    conn,
    config,
    ticket,
    claim,
    *,
    source_result_id,
    action_id,
    observation,
    now,
    recovery_observation=None,
    mixed_observation=None,
    code_effect=None,
    feedback_effect=None,
):
    """Persist a fresh, materialized observation and prove current grant for one exact root action.

    Run within the same writer that decides the local effect transition. A caller
    must still prove its prepared payload, target, ordered receipts and intent.
    """
    tapd = _source_scope(config, ticket, claim)
    if (
        tapd is None
        or not conn.in_transaction
        or observation.facts.identity != ticket
        or not fence.lock_claim(conn, claim, now=now)
    ):
        return None
    required = _required_effect(conn, ticket, source_result_id, action_id)
    if required is None:
        return None
    root, action = required
    observed_at = utc_timestamp(observation.observed_at)
    accepted_at = utc_timestamp(root["accepted_at"])
    if observed_at is None or accepted_at is None or observed_at <= accepted_at:
        return None
    publication = source_observation.publication_facts(conn, ticket)
    current = tapd_intake.observe_ticket(conn, tapd, observation, publication, now=now)
    if source_delivery_resume.is_recovered_tapd_observation(
        recovery_observation
    ) and not source_delivery_resume.recovered_tapd_snapshot_current(recovery_observation, observation):
        return None
    if mixed_observation is not None and not source_delivery_observation.matches(
        conn,
        mixed_observation,
        ticket,
        claim,
        source_result_id,
        action_id,
        revision=current.revision,
        snapshot=observation,
        publication=publication,
        code_effect=code_effect,
        feedback_effect=feedback_effect,
    ):
        return None
    if (
        not current.revision
        or publication.unknown_intents
        or not _unstarted_effect(action)
        or source_results.unknown_github_effect(conn, ticket.source_id)
    ):
        return None
    if root["kind"] == SourceResultKind.CONTROL_NOTICE:
        if (
            _confirmation_notice_current(conn, config, ticket, root, action, current.revision)
            if "confirmation_question_id" in (json_object(root["evidence_json"]) or {})
            else _notice_current(conn, tapd, ticket, root, action, current.revision)
        ):
            return CurrentSourceDeliveryGrant(source_result_id, action_id, current.revision, ())
        return None
    round_ = source_results.delivery_round(conn, ticket.source_id, root["round_id"])
    requests = current_delivery_requests(
        conn,
        config,
        ticket,
        root,
        round_,
        action,
        revision=current.revision,
        recovery_observation=recovery_observation,
        recovery_claim=claim,
        mixed_observation=mixed_observation,
        code_effect=code_effect,
        feedback_effect=feedback_effect,
        now=now,
    )
    if requests:
        return CurrentSourceDeliveryGrant(source_result_id, action_id, current.revision, requests)
    return None


def current_delivery_requests(
    conn,
    config,
    ticket,
    root,
    round_,
    action,
    *,
    revision,
    recovery_observation=None,
    recovery_claim=None,
    mixed_observation=None,
    code_effect=None,
    feedback_effect=None,
    now=None,
):
    """Recheck consumed authority and record any proved mixed cancellation in the caller's writer."""
    tapd = _configured_tapd(config, ticket)
    if tapd is None:
        return ()
    evidence = json_object(root["evidence_json"]) or {}
    if (
        round_ is None
        or round_["round_id"] != root["round_id"]
        or round_["source_id"] != root["source_id"]
        or root["source_id"] != ticket.source_id
        or root["input_version"] != round_["input_version"]
        or evidence.get("round_input_sha256") != round_["input_sha256"]
        or sha256(round_["input_snapshot_json"].encode()).hexdigest() != round_["input_sha256"]
    ):
        return ()
    github_ids = source_members.round_github_request_ids(conn, root["round_id"])
    github = {}
    canceled = intake_queries.round_cancellation(conn, root["round_id"]) is not None
    if github_ids and (canceled or round_["status"] == SourceRoundStatus.CANCELED):
        if (
            not canceled
            or round_["status"] != SourceRoundStatus.CANCELED
            or action["provider"] != "tapd"
            or action["action_type"] != "comment"
            or mixed_observation is not None
            or code_effect is not None
            or feedback_effect is not None
            or now is None
            or recovery_claim is None
        ):
            return ()
        repo_names, repo_id = accepted_delivery_lineage(conn, config, root, round_, action)
        if repo_names is None:
            return ()
        return source_delivery_resume.current_recovered_tapd(
            conn,
            config,
            ticket,
            recovery_claim,
            root,
            round_,
            action,
            recovery_observation,
            revision=revision,
            now=now,
            repo_names=repo_names,
            repo_id=repo_id,
        )
    if github_ids:
        if (
            now is None
            or recovery_claim is None
            or action["provider"] != ("tapd" if code_effect is None and feedback_effect is None else "github")
            or canceled
            or round_["status"] != SourceRoundStatus.ACTIVE
            or recovery_observation is not None
            or not source_delivery_observation.matches(
                conn,
                mixed_observation,
                ticket,
                recovery_claim,
                root["source_result_id"],
                action["action_id"],
                revision=revision,
                code_effect=code_effect,
                feedback_effect=feedback_effect,
            )
        ):
            return ()
        observed = tapd_intake.current_accepted_github_delivery(conn, config, ticket, mixed_observation.github, now=now)
        if observed is None:
            return ()
        if observed.cancellation:
            intake_write.request_cancellation(
                conn, source_id=ticket.source_id, round_id=root["round_id"], reason=observed.cancellation, now=now
            )
            return ()
        github = observed.decisions
        if set(github) != set(github_ids) or not source_delivery_observation.is_current(mixed_observation):
            return ()
    elif mixed_observation is not None or feedback_effect is not None:
        return ()
    elif canceled or round_["status"] == SourceRoundStatus.CANCELED:
        if not (
            canceled
            and round_["status"] == SourceRoundStatus.CANCELED
            and source_delivery_resume.current_effect(
                conn,
                config,
                ticket,
                root["source_result_id"],
                action["action_id"],
                recovery_observation,
                revision=revision,
                claim=recovery_claim,
            )
        ):
            return ()
    elif round_["status"] not in (
        SourceRoundStatus.ACTIVE,
        SourceRoundStatus.EXECUTION_FINISHED,
        SourceRoundStatus.WAITING_FOR_USER,
    ):
        return ()
    repo_names, repo_id = accepted_delivery_lineage(conn, config, root, round_, action)
    if repo_names is None:
        return ()
    members = source_results.consumed_members(conn, root, round_)
    if not members:
        return ()
    if any(
        decision.reason != "allowed"
        or decision.capability != round_["capability"]
        or not repo_names <= set(decision.repos)
        for decision in github.values()
    ):
        return ()
    tapd_members = tuple(row for row in members if row["request_id"] not in github)
    decisions = tapd_intake.delivery_request_decisions(conn, tapd, ticket, tapd_members, revision=revision)
    if len(decisions) != len(tapd_members):
        return ()
    for row, decision in zip(tapd_members, decisions, strict=True):
        if (
            decision.reason != "allowed"
            or decision.capability != round_["capability"]
            or not repo_names <= set(decision.repos)
            or (row["repo_id"] is not None and row["repo_id"] != repo_id)
        ):
            return ()
    return tuple(row["request_id"] for row in members)


def tapd_reconciliation_scope(conn, config, ticket, claim, *, source_result_id, action_id, now):
    """Read one unknown action's exact trusted prepared scope without granting an effect."""
    if (
        _source_scope(config, ticket, claim) is None
        or not conn.in_transaction
        or not fence.lock_claim(conn, claim, now=now)
    ):
        return None
    required = _required_effect(conn, ticket, source_result_id, action_id)
    if required is None:
        return None
    _, action = required
    if (
        action["provider"] != "tapd"
        or action["intent_status"] not in {PublicationStatus.EXECUTING, PublicationStatus.ATTENTION}
        or not action["publication_id"]
        or utc_timestamp(action["intent_started_at"]) is None
    ):
        return None
    prepared = json_object(action["prepared_evidence_json"])
    target = json_object(action["target_json"])
    if prepared is None or prepared.get("action_id") != action_id or prepared.get("target") != target:
        return None
    body, receipt_id, digest = (prepared.get(key) for key in ("body", "receipt_id", "body_digest"))
    principal = prepared.get("publisher_principal")
    version = prepared.get("normalization_version")
    if (
        not isinstance(body, str)
        or not isinstance(receipt_id, str)
        or re.fullmatch(r"rbt-[a-z0-9]{12,64}", receipt_id) is None
        or receipt_id not in body
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(principal, str)
        or not principal.strip()
        or type(version) is not int
        or version != 1
    ):
        return None
    try:
        bot_name = comment_name(prepared.get("bot_name"), "bot_name")
        reply_name = comment_name(prepared.get("reply_name"), "reply_name")
    except ValueError:
        return None
    return TapdReconciliationScope(
        action["publication_id"],
        ticket.key,
        principal,
        PreparedComment(body, receipt_id, digest, version, bot_name, reply_name),
    )


def current_started_github_delivery(
    conn,
    config,
    ticket,
    claim,
    *,
    source_result_id,
    action_id,
    publication_id,
    revision,
    now,
    recovery_observation=None,
    mixed_observation=None,
):
    """Recheck the exact prepared GitHub effect just before its guarded write."""
    if (
        _source_scope(config, ticket, claim) is None
        or not conn.in_transaction
        or not fence.lock_claim(conn, claim, now=now)
    ):
        return None
    row = source_github_publication.required_action(conn, source_result_id, action_id)
    required = _required_effect(conn, ticket, source_result_id, action_id)
    packet = source_github_publication.prepared_request(row) if row is not None else None
    if (
        row is None
        or required is None
        or row["publication_id"] != publication_id
        or row["intent_status"] != PublicationStatus.EXECUTING
        or row["publish_status"] != PublishStatus.NOT_PUBLISHED
        or packet is None
    ):
        return None
    started_effect = (action_id, publication_id)
    root, action = required
    round_ = source_results.delivery_round(conn, ticket.source_id, root["round_id"])
    requests = current_delivery_requests(
        conn,
        config,
        ticket,
        root,
        round_,
        action,
        revision=revision,
        recovery_observation=recovery_observation,
        recovery_claim=claim,
        mixed_observation=mixed_observation,
        code_effect=source_github_observation.code_started(publication_id, packet),
        now=now,
    )
    if not requests:
        return None
    if (
        source_observation.publication_facts(conn, ticket).unknown_intents
        or source_results.unknown_github_effect(
            conn, ticket.source_id, require_positive_receipt=True, started_effect=started_effect
        )
        or source_sessions.blockers_for_publication(
            conn, ticket.source_id, source_result_id, started_effect=started_effect
        )
        or (mixed_observation is not None and not source_delivery_observation.is_current(mixed_observation))
    ):
        return None
    return CurrentSourceDeliveryGrant(source_result_id, action_id, revision, requests)

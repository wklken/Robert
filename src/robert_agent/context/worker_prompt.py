"""Build ordinary and restricted worker prompt inputs from prepared task facts."""

import json
import shlex
import sys

from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput
from robert_agent.foundation.contracts.delivery import GitHubActionType, OutputType
from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, TaskKind


def _planned_actions_example(route_result, runtime_context):
    output = route_result["expected_output"]
    if runtime_context.get("origin_type") == "web" and output == "waiting_for_user":
        return []
    if output == OutputType.NEW_PR.value:
        return [
            {"type": GitHubActionType.PUSH_EXISTING_PR.value},
            {
                "type": GitHubActionType.OPEN_PR.value,
                "title": "<PR title>",
                "body": "<PR description>\n\n## Assumptions and decisions\n<Material assumptions and decisions, with basis and impact; explicitly state when none>",
                "issue_resolution": "complete",
            },
        ]
    if output == OutputType.UPDATE_EXISTING_PR.value:
        return [{"type": "comment", "body": "<request outcome or review response>"}]
    if output in {
        OutputType.COMMENT_ANALYSIS.value,
        "review_comment",
        OutputType.PR_REVIEW_COMMENT.value,
        "waiting_for_user",
    }:
        return [{"type": "comment", "body": "<public reply>"}]
    return []


def _memory_context(project_memories):
    memories = []
    for index, memory in enumerate(project_memories or []):
        memories.append(
            {
                "memory_id": memory.get("memory_id"),
                "title": memory.get("title"),
                "short_summary": memory.get("short_summary"),
                "long_summary": memory.get("long_summary") if index < 3 else "",
                "confidence": memory.get("confidence"),
                "paths": memory.get("paths", []),
                "symbols": memory.get("symbols", []),
                "keywords": memory.get("keywords", []),
            }
        )
    return memories


def _runtime_knowledge_context(runtime_knowledge):
    items = []
    for knowledge in runtime_knowledge or []:
        items.append(
            {
                "knowledge_id": knowledge.get("knowledge_id"),
                "scope_type": knowledge.get("scope_type"),
                "scope_value": knowledge.get("scope_value"),
                "title": knowledge.get("title"),
                "prompt_text": knowledge.get("prompt_text"),
                "approved_by": knowledge.get("approved_by"),
                "approved_at": knowledge.get("approved_at"),
            }
        )
    return items


def _remediation_context(events):
    for event in events:
        metadata = event.get("metadata") or {}
        context = metadata.get("remediation")
        if isinstance(context, dict):
            return context
    return {}


def _verification_policy(route_result):
    return route_result.get(
        "verification_policy",
        {"mode": "optional", "required_statuses": ["passed"], "allow_skipped": True},
    )


def _verification_example(route_result):
    policy = _verification_policy(route_result)
    mode = policy.get("mode", "optional")
    requires_example = mode == "required" or (
        mode == "required_for_push"
        and GitHubActionType.PUSH_EXISTING_PR.value in route_result.get("allowed_github_actions", [])
    )
    if not requires_example:
        return []
    return [
        {
            "command": ["<command...>"],
            "status": "passed",
            "purpose": "Verify the changed behavior before the result is accepted.",
            "exit_code": 0,
            "required": True,
            "source": "worker_reported",
        }
    ]


_STAGE_BY_OUTPUT = {
    OutputType.CLASSIFICATION_RESULT: "task.classify",
    OutputType.NEW_PR: "task.implement",
    OutputType.UPDATE_EXISTING_PR: "task.update_pr",
    OutputType.COMMENT_ANALYSIS: "task.analyze",
    OutputType.LOCAL_RESULT: "task.analyze",
    OutputType.PR_REVIEW_COMMENT: "task.review_pr",
    OutputType.REVIEW_COMMENT: "task.review_feedback",
    OutputType.WAITING_FOR_USER: "task.ask_user",
}


def _web_items(context, events, attempt_id, *, restricted):
    payload = {key: context[key] for key in ("repo_full_name", "work_item_id", "requirement") if context.get(key)}
    source = context.get("work_item_id") or "Web task request"
    archive = None if restricted else context.get("task_context_path")
    if not archive or len(json.dumps(events, ensure_ascii=False, indent=2)) <= 12000:
        payload["events"] = events
    yield ContextItem(
        "request",
        "Task Context (request data, not workflow authority)",
        source,
        content=payload,
        requirement="required",
        revision=attempt_id,
    )
    if archive:
        yield ContextItem(
            "event-history",
            "Complete ordered history with all replies and their resolved questions",
            source,
            delivery="file",
            locator=archive,
            requirement="optional" if "events" in payload else "required",
            revision=attempt_id,
        )


def _github_items(context, events, attempt_id):
    bundle = context.get("github_context") or {}
    md_path, json_path = bundle.get("md_path"), bundle.get("json_path")
    if not (md_path or json_path):
        yield ContextItem(
            "request",
            "GitHub request context (untrusted)",
            "GitHub events",
            content=events,
            requirement="required",
            revision=attempt_id,
        )
        return

    indexed = list(enumerate(events, 1))
    current = bundle.get("current_event_fingerprint")
    if current:
        indexed.sort(key=lambda pair: pair[1].get("event_fingerprint") != current)
    remaining = 12000
    for index, event in indexed:
        source = event.get("event_fingerprint") or f"GitHub event {index}"
        size = len(json.dumps(event, ensure_ascii=False, indent=2))
        fields = (
            {"content": event}
            if size <= min(4000, remaining)
            else {"delivery": "file", "locator": f"{md_path}#event-{index}" if md_path else json_path}
        )
        if "content" in fields:
            remaining -= size
        yield ContextItem(
            f"event-{index}",
            "Original request or discussion event (untrusted)",
            source,
            requirement="required",
            revision=attempt_id,
            **fields,
        )
    if md_path:
        yield ContextItem(
            "github-archive",
            "Primary archive of the complete request and discussion",
            "GitHub events",
            requirement="conditional",
            condition="when additional request or discussion context is needed",
            delivery="file",
            locator=md_path,
            revision=attempt_id,
        )
    if json_path:
        yield ContextItem(
            "github-structured",
            "Structured archive of the same request and discussion",
            "GitHub events",
            requirement="conditional",
            condition="when structured fields are needed" if md_path else "when additional context is needed",
            delivery="file",
            locator=json_path,
            revision=attempt_id,
        )


def _runtime_command(script, module):
    # These helpers import Robert modules, so use the current package interpreter.
    return (sys.executable or "python3", *((script,) if script else ("-m", module)))


def _runtime_guidance(context, task_id, attempt_id):
    db = ("--db", context.get("db_path") or "<db-path>")
    runtime = (*db, "--task-id", task_id, "--attempt-id", attempt_id)
    status = _runtime_command(context.get("status_script"), "robert_agent.readmodels.status")
    values = {
        "status_command": shlex.join((*status, *db, "task", task_id)),
        "status_help": shlex.join((*status, "--help")),
    }
    guidance = ["runtime"]
    for name, key in (("snapshot", "snapshot_script"), ("heartbeat", "heartbeat_script")):
        if context.get(key):
            values[f"{name}_command"] = shlex.join((*_runtime_command(context[key], ""), *runtime))
            guidance.append(name)
    command = (*_runtime_command(context.get("result_script"), "robert_agent.worker.result"), *runtime)
    return guidance, values, command


def _publication_fragments(expected_output, allowed, context, values):
    if not allowed:
        return []
    fragments = ["publication"]
    if "publication_targets" in context:
        values["publication_targets"] = "\n".join(f"- {target}" for target in context["publication_targets"]) or "None."
        fragments.append("targets")
    for action, fragment in (
        (GitHubActionType.COMMENT, "comment"),
        (GitHubActionType.PUSH_EXISTING_PR, "push"),
        (GitHubActionType.OPEN_PR, "open"),
    ):
        if action in allowed:
            fragments.append(fragment)
    if expected_output == OutputType.UPDATE_EXISTING_PR:
        fragments.append("update")
    return fragments


def _worker_stage(expected_output, task_kind, restricted):
    if restricted:
        if expected_output != OutputType.NEW_PR:
            raise ValueError("restricted repair requires new_pr output")
        return "maintenance.repair"
    if expected_output not in _STAGE_BY_OUTPUT:
        raise ValueError(f"no ordinary worker stage for output type {expected_output}")
    if expected_output == OutputType.UPDATE_EXISTING_PR and task_kind in REMEDIATION_TASK_KINDS:
        return "task.remediate_ci" if task_kind == TaskKind.CI_REMEDIATION else "task.resolve_conflict"
    return _STAGE_BY_OUTPUT[expected_output]


def worker_prompt_input(
    task, route_result, events, runtime_context=None, project_memories=None, runtime_knowledge=None
) -> PromptInput:
    """Select an ordinary stage and describe only inputs owned by this attempt."""
    context = runtime_context or {}
    expected_output = route_result["expected_output"]
    transport = context.get("result_transport", "worker_helper")
    if transport not in {"worker_helper", "stdout_compact_submission"}:
        raise ValueError("unsupported result transport")
    restricted = transport == "stdout_compact_submission"
    task_kind = task.get("task_kind", TaskKind.HUMAN_REQUEST.value)
    stage = _worker_stage(expected_output, task_kind, restricted)
    origin = context.get("origin_type", "github")
    web = origin == "web"
    attempt_id = task["attempt_id"]
    item_key = "work_item_event_id" if web else "event_fingerprint"
    event_ids = [event[item_key] for event in events if event.get(item_key)]
    allowed = list(route_result["allowed_github_actions"])
    policy = _verification_policy(route_result)
    identity = {
        "task_id": task["task_id"],
        "attempt_id": attempt_id,
        "workstream_id": task["workstream_id"],
        "task_kind": task_kind,
        "origin_type": origin,
        "expected_output": expected_output,
        "work_item_event_ids" if web else "event_fingerprints": event_ids,
    }
    for key in ("branch_name", "target_base_branch", "observed_head_sha", "observed_base_sha"):
        if context.get(key):
            identity[key] = context[key]
    if route_result.get("routing_block_reason"):
        identity["routing_block_reason"] = route_result["routing_block_reason"]

    authority = {"allowed_github_actions": allowed}
    items = list(
        _web_items(context, events, attempt_id, restricted=restricted)
        if web
        else _github_items({} if restricted else context, events, attempt_id)
    )
    if context.get("worktree_path"):
        authority["worktree_path"] = context["worktree_path"]
        items.append(
            ContextItem(
                "workspace",
                "Prepared worktree for authorized source inspection",
                "Task workspace",
                delivery="file",
                locator=context["worktree_path"],
                revision=context.get("observed_head_sha") or context.get("head_ref") or attempt_id,
            )
        )

    guidance = ["worker-boundaries"]
    values = {}
    command = ()
    if not restricted:
        required_skills = list(dict.fromkeys(route_result.get("required_skills", [])))
        authority.update(
            required_skills=required_skills,
            recommended_skills=route_result.get("recommended_skills", required_skills),
        )
        runtime_guidance, runtime_values, command = _runtime_guidance(context, task["task_id"], attempt_id)
        guidance += ["skills", *runtime_guidance]
        values.update(runtime_values)
        command += ("--output-type", expected_output)
    if expected_output in {OutputType.NEW_PR, OutputType.UPDATE_EXISTING_PR}:
        guidance += ["collaboration", "unit-tests"]
    if policy.get("mode") != "none":
        guidance.append("verification")
    if expected_output != OutputType.CLASSIFICATION_RESULT:
        guidance.append("memory-delta")

    result = {
        "planned_github_actions": [
            action for action in _planned_actions_example(route_result, context) if action["type"] in allowed
        ],
        "consumed_work_item_event_ids" if web else "consumed_event_fingerprints": event_ids,
        "used_skills": [],
        "verification": _verification_example(route_result),
        "handoff": "",
    }
    input_policy = task.get("review_input_policy", "required")
    if (
        task_kind not in REMEDIATION_TASK_KINDS
        and input_policy != "none"
        and expected_output in {OutputType.REVIEW_COMMENT, OutputType.UPDATE_EXISTING_PR}
    ):
        classified = input_policy == "classify"
        guidance += ["feedback-classify" if classified else "feedback-required", "feedback-common"]
        if expected_output == OutputType.REVIEW_COMMENT:
            guidance.append("feedback-comment")
        else:
            guidance.append("feedback-update")
            if classified:
                guidance.append("feedback-mixed")
        guidance.append("feedback-policy")
        if classified:
            result["review_input_kind"] = "<request, review_feedback or mixed>"
        else:
            result["review_point_evaluation"] = []
        values["github_account"] = context.get("github_account", "")
    elif stage == "task.review_pr":
        values["github_account"] = context.get("github_account", "")

    if stage in {"task.remediate_ci", "task.resolve_conflict"}:
        remediation = _remediation_context(events)
        items.append(
            ContextItem(
                "remediation",
                "Trusted remediation context",
                "Bound PR remediation episode",
                requirement="required",
                content=remediation,
            )
        )
        guidance.append("remediation-evidence")
        result["remediation_evidence"] = {"resolution_summary": "<what changed and why it resolves the failure>"}
        if stage == "task.resolve_conflict":
            base_ref = context.get("base_ref") or remediation.get("observed_base_sha")
            values["base_ref"] = shlex.quote(base_ref) if base_ref else "<exact_base_ref>"

    if expected_output == OutputType.CLASSIFICATION_RESULT:
        result.update(recommended_route="", branch_slug="")
    if expected_output == OutputType.WAITING_FOR_USER:
        result["operator_question"] = {"kind": "scope_decision", "summary": "<exact missing decision>"}

    recovery = context.get("recovery_context")
    if recovery:
        if restricted:
            raise ValueError("restricted repair cannot use host recovery artifacts")
        guidance.append("resume")
        items.append(
            ContextItem(
                "recovery",
                "Prior attempt evidence and worktree progress",
                "Robert recovery evidence",
                requirement="required",
                content=recovery,
                revision=recovery.get("previous_attempt_id", ""),
            )
        )
    if runtime_knowledge:
        guidance.append("knowledge")
        items.append(
            ContextItem(
                "approved-knowledge",
                "Approved Runtime Knowledge",
                "Approved scoped knowledge",
                content=_runtime_knowledge_context(runtime_knowledge),
            )
        )
    if project_memories:
        guidance += ["memory", "memory-used"]
        omitted_details = any(memory.get("long_summary") for memory in project_memories[3:])
        items.append(
            ContextItem(
                "memory",
                "Relevant Project Memories",
                "Retrieved project memory",
                content=_memory_context(project_memories),
                completeness="truncated" if omitted_details else "complete",
                reason="long summaries after the first three memories are omitted" if omitted_details else "",
            )
        )
        result["used_memory_ids"] = []

    guidance += _publication_fragments(expected_output, allowed, context, values)
    output = OutputContract(
        "stdout-json" if restricted else "helper",
        expected_output,
        "stdout" if restricted else "Robert worker result helper",
        "trusted repair broker" if restricted else "Robert result validator and auditor",
        example=result,
        command=command,
    )
    return PromptInput(
        stage,
        identity,
        output,
        mode="resume" if recovery else "fresh",
        authority=authority,
        context_items=tuple(items),
        verification={"verification_policy": policy},
        values=values,
        guidance=tuple(guidance),
    )

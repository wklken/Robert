"""One durable targeted repair, then an answerable scoped decision."""

import json
from pathlib import Path

from robert_agent.persistence import transactions
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.execution.stages import StageStopped, attempt_is_active
from robert_agent.security.redaction import redact_and_truncate


def validate_feedback(coordinator):
    """Check pinned feedback without calling coordinator.check_active()."""
    feedback = coordinator.final_feedback
    if feedback is None:
        return
    try:
        with store.connect(coordinator.db_path) as conn:
            entry = store.session(conn, task_id=coordinator.task_id)
        if not entry or entry["round_id"] != coordinator.round_id or entry["request"].get("final_feedback") != feedback:
            raise ValueError("final feedback owner or binding changed")
        store.validate_final_feedback(entry, feedback)
    except (OSError, ValueError) as exc:
        raise StageStopped(str(exc)) from exc


def handle(coordinator, blocked):
    try:
        feedback = {
            **{key: blocked.artifact[key] for key in ("round_id", "snapshot_id", "candidate_sha", "report_sha256")},
            "final_sha": blocked.disposition["head"],
            "verification_path": blocked.artifact["final_verification_path"],
            "verification_sha256": blocked.artifact["final_verification_sha256"],
            "verification_run_id": blocked.artifact["run_id"],
            "verification_input_sha256": blocked.artifact["input_sha256"],
            "source_run_id": blocked.disposition["run_id"],
            "dispositions_path": blocked.disposition["dispositions_path"],
            "dispositions_sha256": blocked.disposition["dispositions_sha256"],
        }
        with store.connect(coordinator.db_path) as conn:
            entry = store.session(conn, task_id=coordinator.task_id)
            if not entry or entry["round_id"] != coordinator.round_id:
                raise ValueError("final feedback owner changed")
            entry["feedback_runs"] = store.feedback_runs(
                conn, coordinator.round_id, {feedback["verification_run_id"], feedback["source_run_id"]}
            )
        value = store.validate_final_feedback(entry, feedback)
        if value != blocked.value:
            raise ValueError("final blocker differs from registered feedback")
    except (KeyError, OSError, ValueError) as exc:
        raise StageStopped(str(exc)) from exc
    count = entry["request"].get("final_repair_count", 0)
    has_blocker = bool(value["blocking_findings"]) or any(
        item["status"] == "blocking" for item in value["checked_findings"]
    )
    if has_blocker and count < 1:
        with store.connect(coordinator.db_path) as conn:
            transactions.begin(conn, immediate=True)
            if not attempt_is_active(conn, coordinator.task_id, coordinator.attempt_id):
                raise StageStopped("final repair owner changed")
            current = store.session(conn, task_id=coordinator.task_id)
            if current["round_id"] != entry["round_id"] or current["request"] != entry["request"]:
                raise StageStopped("final repair request changed")
            request = {**current["request"], "final_repair_count": count + 1, "final_feedback": feedback}
            store.update(conn, coordinator.round_id, request_json=json.dumps(request, sort_keys=True))
        coordinator.request.update(request)
        coordinator.final_feedback = feedback
        return None
    reasons = value["blocking_findings"] + [
        f"{item['id']}: {item['reason']}"
        for item in value["checked_findings"]
        if item["status"] in {"blocking", "unknown"}
    ]
    if not reasons:
        reasons = ["最终复核未批准，需确认原任务的验收依据或提供阻断证据。"]
    summary = redact_and_truncate("最终复核需要确认：\n" + "\n".join(reasons), 1800).get(
        "text", "最终复核需要确认验收或阻断依据。"
    )
    question = {
        "kind": "scope_decision",
        "summary": summary,
        "choices": [
            {
                "id": "clarify",
                "label": "补充验收或修复要求",
                "tradeoff": "提供原任务范围内的验收依据或明确修复要求，再继续复核；保留现有代码，仍需满足真实阻断和必需验证。",
            },
            {
                "id": "defer",
                "label": "暂缓当前交付",
                "tradeoff": "保留当前检查点并暂缓交付，待验收依据和阻断处理明确后继续；当前不会发布 PR。",
            },
        ],
    }
    coordinator.final_decision_feedback = feedback
    return Path(blocked.disposition["dispositions_path"]), question

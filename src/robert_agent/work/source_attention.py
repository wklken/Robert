"""Controller-owned coordination attention within a fenced progress writer."""

import json

from robert_agent.foundation.values import new_id
from robert_agent.persistence.queries import source_attention as read
from robert_agent.persistence.queries import source_results
from robert_agent.persistence.rounds.source_evidence import json_object
from robert_agent.persistence.stores import source_attention as write
from robert_agent.persistence.stores import source_coordination as progress
from robert_agent.persistence.stores import source_sessions as fence
from robert_agent.security.redaction import redact_text

_SUMMARIES = {
    "unsupported_model_profile": "当前配置无法启动来源分析模型。",
    "unsupported_source_model_profile": "当前配置无法启动来源分析模型。",
    "source_stage_failed": "来源分析未能完成。",
    "source_stage_missing_evidence": "来源分析缺少可验证的仓库证据。",
    "source_preparation_failed": "来源任务准备失败。",
    "source_route_unavailable": "当前来源任务缺少可用的执行路由。",
    "source_child_result_invalid": "来源任务结果无法验证。",
    "source_audit_unavailable": "来源任务结果暂未通过审核。",
    "worker_launch_unconfirmed": "无法确认来源任务的 Worker 启动状态。",
    "worker_identity_unconfirmed": "无法确认来源任务的 Worker 进程身份。",
    "worker_exit_unconfirmed": "无法确认来源任务的 Worker 已退出。",
    "source_model_transport_unconfirmed": "无法确认来源分析模型的运行状态。",
    "source_model_exit_unconfirmed": "无法确认来源分析模型已退出。",
    "source_model_identity_unconfirmed": "无法确认来源分析模型的进程身份。",
    "source_model_recovery_changed": "来源分析的恢复状态已变化，需要检查。",
    "source_model_cancellation_changed": "来源分析的取消状态已变化，需要检查。",
    "source_cancellation_unsettled": "来源任务仍有未处理完的取消事项。",
}
_PROGRESS = {"prepared", "dispatched", "stage_recorded", "audited", "publication_pending"}


def _progress_resolves(conn, current, round_id, status, identity):
    if round_id != current["round_id"]:
        return False
    if current["task_id"] is not None:
        return status in {"audited", "draining"} and identity == current["task_id"]
    if current["repo_basis"] in {"round", "selection"}:
        prepared = read.subject(conn, round_id, identity) if status == "prepared" and identity else None
        return prepared is not None and prepared.task_id is not None and prepared.repo_id == current["repo_id"]
    if current["model_run_id"] is not None:
        return status in {"stage_recorded", "draining"} and identity == current["model_run_id"]
    return status in _PROGRESS


def save_coordination_progress(conn, claim, *, round_id, status, reason, identity, repo_id, next_due_at, now):
    """Save progress and any attention transition in the same fenced writer."""
    source_id = claim.source_id
    if status == "attention" and round_id and not read.owner(conn, source_id, round_id, active_only=True):
        round_id = None
    if not progress.save(
        conn, claim, now=now, next_due_at=next_due_at, status=status, reason=reason, round_id=round_id
    ):
        return
    _observe_coordination(conn, source_id, round_id, status, reason, identity, repo_id, now)


def _observe_coordination(conn, source_id, round_id, status, reason, identity, repo_id, now):
    current = read.open_coordination(conn, source_id)
    if current is not None:
        if read.round_settled(conn, source_id, current["round_id"]) or _progress_resolves(
            conn, current, round_id, status, identity
        ):
            write.resolve_coordination(conn, source_id=source_id, notification_id=current["notification_id"], now=now)
        else:
            return
    if status != "attention" or not round_id:
        return
    subject = read.subject(conn, round_id, identity, repo_id=repo_id)
    if subject is None:
        return
    write.open_coordination(
        conn,
        notification_id=new_id("source-attention"),
        source_id=source_id,
        subject=subject,
        cause=reason if reason in _SUMMARIES else "source_coordination_attention",
        version=read.next_version(conn, source_id),
        now=now,
    )


def open_delivered_question(conn, root, action, *, body, now, receipts=None):
    """Record an ordinary question inside its receipt-verified completion savepoint."""
    if root["worker_result_id"] is not None:
        evidence = json_object(root["evidence_json"]) or {}
        subject = read.subject(conn, root["round_id"], evidence.get("attempt_id"), repo_id=evidence.get("repo_id"))
        if subject is None or subject.task_id != evidence.get("task_id") or subject.attempt_id is None:
            raise ValueError("Delivered question has no exact Worker binding")
    else:
        subject = read.Subject(root["round_id"], model_run_id=root["model_run_id"])
    if receipts is not None:
        source = source_results.tapd_source(conn, root["source_id"])
        if source is None:
            raise ValueError("Delivered question has no exact source")
        confirmed = source_results.confirmed_required_effects(
            conn,
            root["source_result_id"],
            source_results.source_target(source),
        )
        sets = source_results.required_action_sets(conn, root, confirmed) if confirmed else None
        if sets is None or confirmed != receipts or sets[0][-1]["action_id"] != action["action_id"]:
            raise ValueError("Delivered question receipt set changed")
    notification_id = new_id("source-attention")
    write.open_question(
        conn,
        notification_id=notification_id,
        root=root,
        action=action,
        subject=subject,
        body=body,
        receipts=source_results.receipt_vector(receipts) if receipts is not None else None,
        version=read.next_version(conn, root["source_id"], kind="ordinary_question"),
        now=now,
    )
    if read.notice_details(conn, notification_id) is None:
        raise ValueError("Delivered question has no exact notification scope")


_RECOVERY_SUMMARIES = {
    "source_delivery_reprocess_required": "原结果的仓库或执行证据已变化，缺少的结果保持暂停，需要重新处理。",
    "source_delivery_original_checkout_unproven": "无法验证原结果的仓库快照，缺少的结果保持暂停，需要重新处理。",
}


def open_delivery_recovery(conn, pause, *, reason, now):
    """Keep stale-result attention separate from the canceled execution round."""
    if reason not in _RECOVERY_SUMMARIES or not conn.in_transaction:
        return False
    root = source_results.for_source(conn, pause["source_id"], pause["source_result_id"])
    if root is None or root["round_id"] != pause["round_id"]:
        return False
    evidence = json_object(root["evidence_json"]) or {}
    subject = read.subject(conn, root["round_id"], evidence.get("attempt_id"), repo_id=evidence.get("repo_id"))
    if subject is None or subject.attempt_id is None or subject.task_id != evidence.get("task_id"):
        return False
    current = read.open_recovery(conn, pause["source_id"])
    if current is not None:
        if all(current[key] == pause[key] for key in ("source_result_id", "generation")) and (
            current["pause_proof_json"] == pause["proof_json"]
        ):
            return True
        write.resolve_recovery(conn, notification_id=current["notification_id"], source_id=pause["source_id"], now=now)
    identifier = new_id("source-attention")
    write.open_recovery(
        conn,
        notification_id=identifier,
        pause=pause,
        subject=subject,
        cause=reason,
        version=read.next_version(conn, pause["source_id"], kind="delivery_recovery"),
        now=now,
    )
    if read.notice_details(conn, identifier) is None:
        raise ValueError("Stale delivery has no exact notification scope")
    return True


def resolve_delivery_recovery(conn, source_id, root_id, generation, *, now):
    current = read.open_recovery(conn, source_id)
    if current and (current["source_result_id"], current["generation"]) == (root_id, generation):
        write.resolve_recovery(conn, notification_id=current["notification_id"], source_id=source_id, now=now)


_EFFECT_SUMMARIES = {
    "write_outcome_unknown": "发布已开始，写入结果需要核实。",
    "readback_not_confirmed": "发布已开始，尚未确认目标端回执。",
    "native_github_effect_unconfirmed": "GitHub 发布已开始，尚未确认目标端回执。",
    "prepared_scope_unavailable": "发布已开始，保存的请求范围暂不可验证。",
    "saved_native_request_unavailable": "发布已开始，保存的请求暂不可验证。",
    "published_receipt_unverified": "已记录发布状态，保存的回执暂不可验证。",
}
_EFFECT_DEFAULT = "source_publication_unconfirmed"


def _publication_episode(conn, claim, root_id, action_id, provider, publication_id, now):
    if not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return None, None
    subject = read.publication_subject(conn, claim.source_id, root_id, action_id, provider)
    if subject is None or (publication_id is not None and subject["publication_id"] != publication_id):
        return None, None
    latest = read.publication_episode(conn, subject["publication_id"])
    if latest is not None and write.publication_binding(latest) != write.publication_binding(subject):
        return None, None
    return subject, latest


def observe_publication_unknown(conn, claim, *, root_id, action_id, provider, reason, now, publication_id=None):
    """Record durable uncertainty under the existing source fence and effect scope."""
    subject, latest = _publication_episode(conn, claim, root_id, action_id, provider, publication_id, now)
    if subject is None or (latest is not None and latest["resolved_at"] is None):
        return
    if subject["intent_status"] == "published" and read.publication_confirmed(conn, subject):
        return
    write.open_publication(
        conn,
        notification_id=new_id("source-attention"),
        subject=subject,
        cause=reason if reason in _EFFECT_SUMMARIES else _EFFECT_DEFAULT,
        version=1 if latest is None else latest["version"] + 1,
        now=now,
    )


def confirm_publication(conn, claim, *, root_id, action_id, provider, publication_id, now):
    """Resolve only the exact alert backed by an independently verified receipt."""
    subject, latest = _publication_episode(conn, claim, root_id, action_id, provider, publication_id, now)
    if (
        subject is not None
        and latest is not None
        and latest["resolved_at"] is None
        and read.publication_confirmed(conn, subject)
    ):
        write.resolve_publication(conn, notification_id=latest["notification_id"], subject=subject, now=now)


def _publication_details(conn, notification_id, include_closed):
    row = read.publication_notice_details(conn, notification_id, include_closed=include_closed)
    if row is None:
        return None
    return {
        "event_type": read.PUBLICATION_NOTICE_TYPE,
        "version": row["version"],
        "reason_code": row["cause"] if row["cause"] in _EFFECT_SUMMARIES else _EFFECT_DEFAULT,
        "summary": _EFFECT_SUMMARIES.get(row["cause"], "发布效果尚未确认。"),
        "required_action": "请在本地检查发布回执。ack 仅记录收悉。",
        "target": {
            "provider": "tapd",
            "host": row["host"],
            "workspace_id": row["project_key"],
            "source_id": row["source_id"],
            "attention_id": notification_id,
            "source_result_id": row["source_result_id"],
            "action_id": row["action_id"],
            "publication_id": row["publication_id"],
            "publication_provider": row["provider"],
            **({"round_id": row["round_id"]} if row["round_id"] is not None else {}),
            **({"repo_id": row["repo_id"]} if row["repo_id"] is not None else {}),
            **({"repo_ids": json.loads(row["repo_ids_json"])} if row["repo_ids_json"] is not None else {}),
        },
    }


def notification_details(conn, notification_id, *, include_closed=False):
    row = read.notice_details(conn, notification_id, include_closed=include_closed)
    if row is None:
        return _publication_details(conn, notification_id, include_closed)
    question = row["kind"] == "ordinary_question"
    recovery = row["kind"] == "delivery_recovery"
    summary = _SUMMARIES.get(row["cause"], "来源协调需要人工检查。")
    event_type = read.NOTICE_TYPE
    reason = row["cause"] if row["cause"] in _SUMMARIES else "source_coordination_attention"
    required_action = "请在本地查看来源状态。ack 仅记录收悉。"
    if question:
        safe = redact_text(row["question_body"], strict_assignments=True)
        summary = safe["text"][:2000] if safe["ok"] else "请在 TAPD 查看已交付的问题。"
        event_type = read.QUESTION_NOTICE_TYPE
        reason = "source_question_delivered"
        required_action = "请在 TAPD 查看已交付的问题。ack 仅记录收悉。"
    elif recovery:
        summary = _RECOVERY_SUMMARIES.get(row["cause"], "原结果缺少当前证据，补发保持暂停。")
        event_type = read.RECOVERY_NOTICE_TYPE
        reason = row["cause"] if row["cause"] in _RECOVERY_SUMMARIES else "source_delivery_reprocess_required"
        required_action = "请检查原结果并明确请求重新处理。ack 仅记录收悉，不会重跑 Worker 或补发结果。"
    return {
        "title": row["title"],
        "event_type": event_type,
        "version": row["version"],
        "reason_code": reason,
        "summary": summary,
        "required_action": required_action,
        "target": {
            "provider": "tapd",
            "host": row["host"],
            "workspace_id": row["project_key"],
            "source_id": row["source_id"],
            "round_id": row["round_id"],
            "attention_id": notification_id,
            **({"repo_id": row["repo_id"]} if row["repo_id"] is not None else {}),
            **({"source_result_id": row["source_result_id"]} if question else {}),
            **(
                {"source_result_id": row["recovery_result_id"], "generation": row["recovery_generation"]}
                if recovery
                else {}
            ),
            **({"repo_ids": json.loads(row["question_repos"])} if row["question_repos"] is not None else {}),
        },
    }

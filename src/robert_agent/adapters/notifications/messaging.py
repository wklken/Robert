"""Bounded external transports for the versioned operator message envelope."""

import json
import os
import shlex
import subprocess


EVENT_LABELS = {
    "execution_delayed": "任务进度延迟",
    "worker_resume_prepared": "已准备恢复执行",
    "worker_stale": "执行进程心跳异常",
    "worker_timeout": "任务执行超时",
    "worker_process_exited": "执行进程退出",
    "worker_cleanup_pending": "无法确认执行进程已停止",
    "worker_failed": "任务执行失败",
    "worker_startup_failed": "执行进程启动失败",
    "worker_authentication_required": "需要修复登录凭据",
    "worker_permission_required": "需要检查访问权限",
    "worker_configuration_required": "需要修复执行配置",
    "worker_branch_conflict": "需要处理分支冲突",
    "execution_attention_required": "任务执行需要检查",
    "operator_question": "需要你回答",
    "operator_decision_required": "需要你决策",
    "completion_acceptance_requested": "需要验收",
    "publication_failed": "发布失败",
    "github_publish_failed": "发布到 GitHub 失败",
    "github_publish_skipped": "已跳过 GitHub 发布",
    "dispatch_created": "已创建请求，等待分类和执行",
    "dispatch_attention": "请求处理需要检查",
    "dispatch_source_closed": "原始请求已关闭，未开始执行",
    "dispatch_completed": "请求已完成，结果已发布",
    "supervisor_unobservable": "监督服务无法读取数据库",
    "supervision_question": "监督服务需要你回答",
    "supervision_notice": "监督服务通知",
    "supervision_incident": "监督服务发现异常",
    "supervision_decision_unavailable": "监督服务暂时无法作出自动决策",
    "control_plane_failed": "调度服务异常",
    "repository_pipeline_failed": "仓库处理流程异常",
}

LIFECYCLE_LABELS = {
    "detected": "已发现请求",
    "authorized": "已确认请求权限",
    "classified": "已完成请求分类",
    "queued": "等待执行",
    "running": "正在执行",
    "active": "正在处理",
    "completed": "已完成",
    "waiting_for_user": "等待你回复",
    "failed": "执行失败",
    "canceled": "已取消",
    "ignored": "已忽略",
}

ROUTE_LABELS = {
    "local-result": "生成本地结果",
    "comment-analysis": "分析请求并回复评论",
    "new-pr": "提交新的代码修改",
    "update-existing-pr": "更新已有的代码修改",
    "review-comment": "处理代码审查意见",
    "review-pr": "审查代码修改",
    "classification-result": "请求分类",
    "waiting-for-user": "等待你补充信息",
    "vote": "汇总多方评估意见",
}

REASON_LABELS = {
    "queue_delayed": "任务排队超过 5 分钟，尚未记录正在执行的 Worker。",
    "publication_delayed": "已审核通过的操作超过 2 分钟仍未发布。",
    "exited": "模型命令以非零退出码结束，未取得有效决策；请查看监督状态中的错误详情。",
    "invalid_output": "模型输出未通过决策格式校验，不能执行自动操作。",
    "timed_out": "模型调用超过允许的时长，未取得有效决策。",
    "stopped": "模型调用已停止，未取得有效决策。",
    "launch_failed": "模型命令无法启动，请检查命令和监督配置。",
    "process_attention": "模型进程身份或停止状态无法确认，需要检查监督状态。",
    "provider_rate_limited": "模型服务触发请求频率限制，将按冷却时间和调用预算等待。",
    "failed_worker_process_exited": "执行进程意外退出；具体退出原因请查看执行日志。",
    "failed_timeout": "任务执行超过允许的时长，已中断本次执行。",
    "failed_worker_startup": "执行进程启动后未在规定时间内报告进展。",
    "failed_transport_retries_exhausted": "连接中断后多次恢复仍失败，已停止自动重试。",
    "needs_resume": "本次执行中断，需要检查能否继续。",
    "authentication_required": "登录凭据无效或已过期，需要重新登录或修复凭据。",
    "permission_required": "访问被拒绝，需要检查仓库或服务权限。",
    "configuration_required": "执行配置不符合要求，需要检查配置。",
    "branch_conflict": "分支状态冲突，需要检查当前分支和远端修改。",
    "rate_limited": "服务触发了请求频率限制，当前请求无法继续。",
    "transient_failure": "本次调用失败，被归类为临时故障；具体原因请查看执行日志。",
    "retry_scheduled": "已安排稍后重试。",
    "retry_budget_exhausted": "已达到重试次数或时间上限，停止自动重试。",
    "recovery_budget_exhausted": "已达到自动恢复上限，需要人工检查。",
    "observation_unavailable": "监督服务无法读取数据库，当前无法确认任务进展。",
    "control_paused": "调度服务已暂停，任务暂时无法继续。",
    "control_child_attention": "调度子进程状态无法确认，需要人工检查。",
    "control_plane_failed": "调度服务运行失败，需要查看调度日志。",
    "repository_pipeline_failed": "仓库处理流程失败，需要查看任务详情。",
    "stalled": "持续观察未发现预期进展，需要检查任务是否停滞。",
    "waiting_for_user": "任务正在等待你的答复。",
    "waiting_for_model_decision": "正在等待模型判断下一步处理方式。",
    "ask_user": "监督服务需要你补充信息。",
    "notify_user": "监督服务有情况需要你知悉。",
    "no_progress": "未发现可用于继续执行的进展。",
    "unrecoverable": "当前证据不满足自动恢复条件，需要人工检查。",
    "orphaned_command_running": "先前启动的命令可能仍在运行，需要先确认其状态。",
    "worker_identity_attention": "执行进程身份或停止状态无法确认，需要人工检查。",
    "worker_identity_unverified": "无法核实执行进程身份。",
    "worker_exited_without_result": "执行进程已退出，但未找到已保存的执行结果。",
    "terminal_task_worker_still_alive": "任务已结束，但执行进程仍在运行。",
    "result_audit_requires_attention": "执行结果未通过审核，需要检查结果和审核记录。",
    "result_waiting_ingestion": "执行结果已保存，等待系统接收。",
    "result_waiting_audit": "执行结果等待审核。",
    "result_waiting_worker_exit": "执行结果已到达，等待确认执行进程退出。",
    "result_contract_cutover": "结果格式要求已变更，需要按当前要求重新处理。",
    "source_closed_before_publication": "原始请求已关闭，本次发布已停止。",
    "publication_scope_stale": "发布所依据的任务状态已变更，需要重新检查。",
    "native_review_checkpoint": "找到了已保存的代码审查进展。",
}

RECOVERY_LABELS = {
    "recoverable_progress": "发现已保存的执行进展，可作为继续执行的依据；尚不代表恢复成功。",
    "transport_interrupted": "模型连接中断，需要检查能否重新连接并继续执行。",
    "no_progress": "未发现可用于恢复的执行进展，需要进一步检查。",
    "orphaned_command_running": "先前启动的命令可能仍在运行，尚不能安全恢复。",
    "unrecoverable": "当前证据不满足自动恢复条件，需要人工检查。",
    "rate_limited": "服务触发请求频率限制，需要按现有策略等待或切换模型。",
    "waiting_for_model_decision": "正在等待模型判断下一步恢复方式。",
    "proposed": "已提出恢复方案，尚未执行。",
    "requested": "已提交恢复请求，等待处理。",
    "applying": "正在执行恢复操作。",
    "observing": "恢复操作后正在观察进展，尚未确认成功。",
    "succeeded": "恢复操作已验证成功。",
    "failed": "恢复操作失败，需要检查原因。",
    "rejected": "恢复请求被拒绝，未执行。",
    "unknown": "恢复结果尚无法确认，需要查看任务详情。",
}

ACTION_LABELS = {
    "ack": "确认收到（仅记录收悉）",
    "reply": "回复问题",
    "cancel": "取消任务",
    "approve": "批准请求",
    "retry": "重试任务",
    "retry_publish": "重试发布",
}


def configured(config=None):
    return bool(os.environ.get("ROBERT_NOTIFICATION_COMMAND") or (config or {}).get("messaging", {}).get("enabled"))


def render_message(payload):
    lines = ["Robert: " + EVENT_LABELS.get(payload["type"], "新通知，请查看任务详情")]
    descriptions = {
        "task_status": LIFECYCLE_LABELS.get(payload.get("task_status"), "状态暂未归类，请查看任务详情"),
        "workstream_status": LIFECYCLE_LABELS.get(payload.get("workstream_status"), "状态暂未归类，请查看任务详情"),
        "route_id": ROUTE_LABELS.get(payload.get("route_id"), "任务类型暂未归类，请查看任务详情"),
        "reason_code": REASON_LABELS.get(payload.get("reason_code"), "原因暂未归类，请查看任务详情中的诊断记录。"),
        "recovery_status": RECOVERY_LABELS.get(payload.get("recovery_status"), "恢复状态暂未归类，请查看任务详情。"),
    }
    for key, name in (
        ("repository", "仓库"),
        ("title", "任务"),
        ("summary", "说明"),
        ("occurred_at", "发生时间"),
        ("task_status", "任务当前状态"),
        ("workstream_status", "工作流当前状态"),
        ("route_id", "任务类型"),
        ("issue_url", "Issue"),
        ("pr_url", "PR"),
        ("result_url", "结果"),
        ("reason_code", "原因"),
        ("recovery_status", "恢复状态"),
        ("safe_error", "错误详情"),
        ("exit_code", "退出码"),
        ("workstream_id", "工作流"),
        ("decision_id", "决策记录"),
        ("required_action", "需要你处理"),
    ):
        if payload.get(key):
            lines.append(f"{name}: {descriptions.get(key, payload[key])}")
    for choice in payload.get("choices", []):
        lines.append(f"{choice['id']}: {choice['label']}")
    if payload.get("task_id"):
        lines.append("查看任务: /robert-task " + payload["task_id"])
    if payload.get("request_id"):
        lines.append("/robert-inbox status " + payload["request_id"])
    if payload.get("notification_id") and payload.get("actions", ["ack"]):
        lines.append("可执行命令:")
        for action in payload.get("actions", ["ack"]):
            lines.append(ACTION_LABELS.get(action, "执行下列操作"))
            suffix = " <answer>" if action == "reply" else ""
            lines.append(f"/robert-respond {payload['notification_id']} {payload.get('version', 0)} {action}{suffix}")
    lines.append("Delivery: " + payload["delivery_id"])
    return "\n".join(lines)


def send(payload, *, config=None, command=None, runner=subprocess.run):
    command = command or os.environ.get("ROBERT_NOTIFICATION_COMMAND")
    if command:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        kwargs = {"input": json.dumps(payload, ensure_ascii=False), "timeout": 5}
    else:
        settings = (config or {}).get("messaging", {})
        if not settings.get("enabled") or settings.get("provider") != "openclaw":
            return False
        argv = [
            "openclaw",
            "message",
            "send",
            "--channel",
            settings["channel"],
            "--account",
            settings["account"],
            "--target",
            settings["target"],
            "--message",
            render_message(payload),
            "--json",
        ]
        if settings.get("thread"):
            argv += ["--thread-id", settings["thread"]]
        kwargs = {"timeout": 30}
    try:
        result = runner(argv, text=True, capture_output=True, check=False, **kwargs)
        if result.returncode != 0:
            return False
        if not command:
            # The CLI's successful JSON contract varies by channel. Explicit
            # negative acknowledgements must never be treated as delivery.
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or response.get("ok") is False:
                return False
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False

"""Read and redact durable notification envelopes without changing lifecycle."""

import json

from robert_agent.persistence import records
from robert_agent.persistence.queries import notifications, repos, dispatch_requests
from robert_agent.security.redaction import redact_text
from robert_agent.work import messaging
from robert_agent.work.messaging_identity import authenticate
from robert_agent.work.errors import WorkItemValidationError


def _redact_values(value):
    # Redact values before JSON serialization: a path regex applied to encoded
    # JSON can consume closing quotes and corrupt the envelope.
    if isinstance(value, str):
        safe = redact_text(value)
        if not safe["ok"]:
            raise ValueError("Message contains sensitive text.")
        return safe["text"]
    if isinstance(value, list):
        return [_redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_values(item) for key, item in value.items()}
    return value


def allowed_repo_ids(conn, config):
    names = (config or {}).get("messaging", {}).get("repos", [])
    configured = {repo["full_name"] for repo in (config or {}).get("repos", [])}
    return frozenset(row[0] for row in repos.load_repos(conn) if row[1] in names and row[1] in configured)


def envelope(conn, notification_id, *, config=None, include_closed=False):
    row = records.first(
        notifications.find_task_id_and_notification_type_and_status_and_metadata_json_by_notification_id(
            conn, notification_id
        )
    )
    if not row:
        return None
    task_id, kind, status, raw = row
    metadata = json.loads(raw or "{}")
    dispatch = dispatch_requests.for_task(conn, task_id) if task_id and config is not None else None
    bound_identity = metadata.get("dispatch_identity") or (json.loads(dispatch["identity_json"]) if dispatch else None)
    if bound_identity and config is not None:
        try:
            authenticate(config, bound_identity)
        except WorkItemValidationError:
            return None
    repo_id, repository_scoped = messaging.notification_scope(conn, task_id, metadata)
    if config is not None and repository_scoped:
        if repo_id not in allowed_repo_ids(conn, config):
            return None
    payload = {
        "protocol": "robert.message.v1",
        "notification_id": notification_id,
        "task_id": task_id,
        "type": kind,
        "version": 0,
        "actions": ["ack"],
        "status": status,
    }
    context = records.first(notifications.find_message_context(conn, notification_id))
    if kind == "supervision_decision_unavailable":
        # Older pending alerts contain workstream context and stderr. A global
        # service alert must stay generic even when replaying those records.
        metadata = {
            key: metadata[key] for key in ("reason_code", "decision_id", "exit_code", "retryable") if key in metadata
        }
        if context and context["occurred_at"]:
            payload["occurred_at"] = context["occurred_at"]
        context = None
    if context:
        for key in (
            "occurred_at",
            "repository",
            "title",
            "task_status",
            "route_id",
            "workstream_id",
            "workstream_status",
        ):
            if context[key]:
                payload[key] = context[key]
        if context["html_url"]:
            payload["pr_url" if context["source_type"] == "pull_request" else "issue_url"] = context["html_url"]
        failure = json.loads(context["failure_json"] or "{}")
        recovery = failure.get("recovery") or {}
        payload["reason_code"] = failure.get("reason_code") or failure.get("original_status") or failure.get("status")
        if recovery.get("status"):
            payload["recovery_status"] = recovery["status"]
        if failure.get("safe_error"):
            payload["safe_error"] = str(failure["safe_error"])[:2000]
    for key in (
        "request_id",
        "summary",
        "issue_url",
        "pr_url",
        "result_url",
        "reason_code",
        "error_class",
        "required_action",
        "retryable",
        "incident_id",
        "workstream_id",
        "decision_id",
        "exit_code",
    ):
        if key in metadata:
            payload[key] = metadata[key]
    if metadata.get("safe_error"):
        payload["safe_error"] = str(metadata["safe_error"])[:2000]
    if metadata.get("request_id"):
        payload["actions"] = []
    if metadata.get("attention_event_id"):
        attention = messaging.attention_details(conn, metadata, include_closed=include_closed)
        if attention is None:
            return None
        payload.update(attention)
        payload["actions"] = messaging.actions_for(attention["event_type"], attention["kind"])
    if kind == "supervision_decision_unavailable":
        payload["summary"] = (
            metadata.get("summary") or "自动决策调用连续失败。这是全局监督服务告警，Kanban 不会生成任务确认项。"
        )
        payload["required_action"] = (
            "在 /operations 通知中心或 robert supervisor status 中检查决策服务；ack 仅记录收悉，后续有效模型调用会自动解除告警。"
        )
        if status == "resolved":
            payload["summary"] = "自动决策服务已恢复；这条告警保留为历史记录。"
            payload["required_action"] = "无需确认或批准操作；ack 仅记录收悉，不会重试任务。"
    if not payload.get("summary"):
        payload["summary"] = {
            "worker_resume_prepared": "已准备恢复执行；这条通知表示准备完成，后续进展请查看任务详情。",
            "worker_stale": "Worker 心跳超过等待阈值，需要查看当前任务和执行记录。",
            "worker_timeout": "Worker 执行超时，需要查看失败原因和恢复状态。",
            "worker_process_exited": "Worker 进程已退出，需要查看执行结果和失败记录。",
            "worker_cleanup_pending": "无法确认执行进程已停止，请检查执行进程和停止记录；确认停止前会继续保留任务占用。",
        }.get(kind, "Robert 记录了一条新通知，请结合当前状态和原因查看详情。")
    if not payload.get("required_action"):
        actions = payload["actions"]
        if actions == ["ack"]:
            payload["required_action"] = "查看详情后可确认收到；ack 仅记录收悉，不会解决故障、重试任务或批准修复。"
        elif "reply" in actions:
            payload["required_action"] = "请使用 reply 命令回答上面的问题，或使用 cancel 取消任务。"
        elif "approve" in actions:
            payload["required_action"] = "请阅读请求后使用 approve 批准，或使用 cancel 取消。"
        elif actions:
            payload["required_action"] = "请查看详情，并按下面列出的命令处理。"
    try:
        return _redact_values(payload)
    except ValueError:
        pass
    # Keep identity, but withhold all free text and mutating actions when the
    # actual decision cannot be safely presented.
    return {
        "protocol": "robert.message.v1",
        "notification_id": notification_id,
        "task_id": task_id,
        "type": kind,
        "version": payload["version"],
        "status": status,
        "actions": ["ack"],
        "summary": "Details require local inspection.",
    }


def delivery_repo_ids(conn, config=None):
    """None leaves custom-command delivery unscoped; an empty set denies repositories."""
    if not (config or {}).get("messaging", {}).get("enabled"):
        return None
    return allowed_repo_ids(conn, config)

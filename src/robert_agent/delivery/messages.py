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
    ):
        if key in metadata:
            payload[key] = metadata[key]
    payload["safe_error"] = str(metadata.get("safe_error", ""))[:2000]
    if metadata.get("request_id"):
        payload["actions"] = []
    if metadata.get("attention_event_id"):
        attention = messaging.attention_details(conn, metadata, include_closed=include_closed)
        if attention is None:
            return None
        payload.update(attention)
        payload["actions"] = messaging.actions_for(attention["event_type"], attention["kind"])
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

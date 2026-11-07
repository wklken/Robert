"""Authenticated local chat ingress; lifecycle remains owned by work commands."""

from contextlib import closing, contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path

from robert_agent.delivery import messages
from robert_agent.security.redaction import redact_text
from robert_agent.work import messaging
from robert_agent.work.application import commands
from robert_agent.work.errors import WorkItemConflictError, WorkItemValidationError, WorkItemNotFoundError
from robert_agent.work.inputs import CommandContext, MAX_REPLY, validate_text


@contextmanager
def _connection(config, *, write=False):
    mode = "rw" if write else "ro"
    uri = Path(config["db_path"]).expanduser().resolve().as_uri() + "?mode=" + mode
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            yield conn
    except sqlite3.Error as exc:
        raise WorkItemValidationError("Messaging storage is unavailable; inspect Robert locally.") from exc


def _authenticate(config, request):
    settings = config.get("messaging", {})
    if not settings.get("enabled") or request.get("protocol") != "robert.message.v1":
        raise WorkItemValidationError("Messaging is disabled or the protocol is unsupported.")
    for field in ("provider", "channel", "account", "conversation", "thread"):
        if request.get(field, "") != settings.get(field, ""):
            raise WorkItemValidationError("Messaging identity is not authorized.")
    if request.get("sender_id") not in settings["allowed_senders"]:
        raise WorkItemValidationError("Messaging identity is not authorized.")
    return f"{settings['provider']}:{settings['channel']}:{settings['account']}:{request['sender_id']}"


def inbox(config, request):
    _authenticate(config, request)
    with _connection(config) as conn:
        scope, params = messages.pending_filter(conn, config)
        rows = conn.execute(
            """SELECT notification_id FROM notifications
            WHERE channel='local' AND status!='resolved'
            AND (json_extract(metadata_json,'$.operator_response') IS NULL
                 OR json_extract(metadata_json,'$.attention_event_id') IS NOT NULL)
            """
            + scope
            + " ORDER BY created_at DESC,notification_id DESC LIMIT 100",
            params,
        ).fetchall()
        entries = [messages.envelope(conn, row[0], config=config) for row in rows]
    return {"ok": True, "messages": [entry for entry in entries if entry]}


def respond(config, request):
    identity = _authenticate(config, request)
    notification_id = validate_text(request.get("notification_id"), "notification_id", 256, required=True)
    version = request.get("version")
    if type(version) is not int or version < 0:
        raise WorkItemValidationError("version must be a nonnegative integer")
    action = request.get("action")
    if action not in {"reply", "approve", "retry", "retry_publish", "cancel", "ack"}:
        raise WorkItemValidationError("Unsupported messaging action.")
    body = validate_text(request.get("body", ""), "body", MAX_REPLY)
    safe = redact_text(body)
    if not safe["ok"]:
        raise WorkItemValidationError("Response contains sensitive text; use local controls.")
    body = safe["text"]
    request_key = (
        "message:"
        + hashlib.sha256(
            json.dumps([notification_id, version, identity, action, body], ensure_ascii=False).encode()
        ).hexdigest()
    )
    with _connection(config) as conn:
        row = conn.execute(
            "SELECT task_id,metadata_json FROM notifications WHERE notification_id=? AND channel='local'",
            (notification_id,),
        ).fetchone()
        if not row:
            raise WorkItemNotFoundError("Message not found.")
        metadata = json.loads(row[1] or "{}")
        repo_id = messaging.notification_scope(conn, row[0], metadata)
        allowed = messages.allowed_repo_ids(conn, config)
        if (repo_id is not None or row[0] or metadata.get("work_item_id")) and repo_id not in allowed:
            raise WorkItemNotFoundError("Message not found.")
        displayed = messages.envelope(conn, notification_id, config=config, include_closed=True)
        if not displayed or action not in displayed["actions"]:
            raise WorkItemValidationError("This message does not authorize that action.")
    if action != "ack":
        context = CommandContext(
            "operator", identity, allowed, frozenset(worker["name"] for worker in config["workers"])
        )
        result = commands.execute_command(
            config["db_path"],
            metadata["work_item_id"],
            context=context,
            command=action,
            expected_version=version,
            expected_event_id=metadata["attention_event_id"],
            idempotency_key=request_key,
            body=body,
        )
        # The command event itself is the atomic receipt; crash/replay reuses it.
        return {
            "ok": True,
            "status": "accepted",
            "notification_id": notification_id,
            "task_id": result.get("task_id"),
            "version": result["item"]["version"],
        }
    # Acknowledgement only records receipt; it never resolves an incident or task.
    with _connection(config, write=True) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT metadata_json FROM notifications WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if not row:
            raise WorkItemNotFoundError("Message not found.")
        metadata = json.loads(row[0] or "{}")
        previous = metadata.get("operator_response")
        if previous and previous["request_key"] != request_key:
            raise WorkItemConflictError("Message already acknowledged.")
        if not previous:
            metadata["operator_response"] = {
                "request_key": request_key,
                "actor_identity": identity,
                "action": action,
                "body": body,
                "created_at": commands.utc_now(),
            }
            conn.execute(
                "UPDATE notifications SET metadata_json=? WHERE notification_id=?",
                (json.dumps(metadata, ensure_ascii=False), notification_id),
            )
    return {"ok": True, "status": "acknowledged", "notification_id": notification_id}

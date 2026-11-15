"""Authenticated local chat ingress; lifecycle remains owned by work commands."""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path

from robert_agent.persistence import database, errors, records, transactions
from robert_agent.persistence.queries import notifications
from robert_agent.persistence.stores import notifications as notification_store

from robert_agent.delivery import messages
from robert_agent.security.redaction import redact_text
from robert_agent.work import messaging
from robert_agent.work.messaging_identity import authenticate
from robert_agent.work.application import commands
from robert_agent.work.errors import WorkItemConflictError, WorkItemValidationError, WorkItemNotFoundError
from robert_agent.work.inputs import CommandContext, MAX_REPLY, validate_text


@contextmanager
def _connection(config, *, write=False):
    mode = "rw" if write else "ro"
    uri = Path(config["db_path"]).expanduser().resolve().as_uri() + "?mode=" + mode
    try:
        with closing(database.connect(uri, uri=True)) as conn:
            yield conn
    except errors.Error as exc:
        raise WorkItemValidationError("Messaging storage is unavailable; inspect Robert locally.") from exc


def inbox(config, request):
    identity = authenticate(config, request)
    with _connection(config) as conn:
        access = messages.notification_access(conn, config, operator_identity=identity)
        rows = notifications.find_inbox(
            conn,
            messaging_repo_ids=access.repo_ids,
            messaging_settings=config["messaging"],
            tapd_project_repos=access.tapd_project_repos,
            tapd_projects=access.tapd_projects,
        )
        entries = [messages.envelope(conn, row[0], config=config, operator_identity=identity) for row in rows]
    return {
        "ok": True,
        "messages": [entry for entry in entries if entry],
        **(
            {
                "dispatch_help": "/robert-inbox new <owner/repo> <request>",
                "dispatch_repos": config["messaging"]["repos"],
            }
            if config["messaging"].get("dispatch_enabled")
            else {}
        ),
    }


def respond(config, request):
    identity = authenticate(config, request)
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
        scope, metadata, access = _response_target(conn, config, identity, notification_id)
        if scope.kind == "tapd" and action != "ack":
            raise WorkItemValidationError("Native notification commands are unavailable.")
        displayed = messages.envelope(
            conn, notification_id, config=config, include_closed=True, operator_identity=identity
        )
        if not displayed or action not in displayed["actions"]:
            raise WorkItemValidationError("This message does not authorize that action.")
    if action != "ack":
        context = CommandContext(
            "operator", identity, access.repo_ids, frozenset(worker["name"] for worker in config["workers"])
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
        transactions.begin_immediate(conn)
        current_identity = authenticate(config, request)
        current, metadata, _access = _response_target(conn, config, current_identity, notification_id)
        if current_identity != identity or current != scope:
            raise WorkItemNotFoundError("Message not found.")
        if current.kind == "tapd":
            displayed = messages.envelope(
                conn, notification_id, config=config, include_closed=True, operator_identity=current_identity
            )
            if not displayed or "ack" not in displayed["actions"]:
                raise WorkItemValidationError("This message does not authorize that action.")
            if version != displayed["version"]:
                raise WorkItemConflictError("Message version changed.")
        previous = metadata.get("operator_response")
        if current.attention_id is not None and displayed["status"] == "resolved" and not previous:
            raise WorkItemConflictError("Message is no longer open.")
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
            notification_store.set_metadata_json_by_notification_id(
                conn, json.dumps(metadata, ensure_ascii=False), notification_id
            )
    return {"ok": True, "status": "acknowledged", "notification_id": notification_id}


def _response_target(conn, config, identity, notification_id):
    row = records.first(notifications.find_local_response(conn, notification_id))
    if row is None:
        raise WorkItemNotFoundError("Message not found.")
    scope = messaging.notification_scope(conn, notification_id)
    access = messages.notification_access(conn, config, operator_identity=identity)
    if not access.allows(scope):
        raise WorkItemNotFoundError("Message not found.")
    return scope, json.loads(row[1] or "{}"), access

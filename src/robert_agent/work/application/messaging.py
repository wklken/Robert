"""Authenticated local chat ingress; lifecycle remains owned by work commands."""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import re

from robert_agent.persistence import database, errors, records, transactions
from robert_agent.persistence.queries import notifications, work_item_events
from robert_agent.persistence.stores import notifications as notification_store

from robert_agent.delivery import messages
from robert_agent.delivery.lock import task_publication_lock
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
    authenticate(config, request)
    with _connection(config) as conn:
        rows = notifications.find_inbox(
            conn, messaging_repo_ids=messages.allowed_repo_ids(conn, config), messaging_settings=config["messaging"]
        )
        entries = [messages.envelope(conn, row[0], config=config) for row in rows]
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


def respond(config, request, *, idempotency_key=None):
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
    if idempotency_key is not None:
        request_key = idempotency_key
    with _connection(config) as conn:
        row = records.first(notifications.find_local_response(conn, notification_id))
        if not row:
            raise WorkItemNotFoundError("Message not found.")
        metadata = json.loads(row[1] or "{}")
        repo_id, repository_scoped = messaging.notification_scope(conn, row[0], metadata)
        allowed = messages.allowed_repo_ids(conn, config)
        if repository_scoped and repo_id not in allowed:
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
        transactions.begin_immediate(conn)
        row = records.first(notifications.find_metadata_json_by_notification_id(conn, notification_id))
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
            notification_store.set_metadata_json_by_notification_id(
                conn, json.dumps(metadata, ensure_ascii=False), notification_id
            )
    return {"ok": True, "status": "acknowledged", "notification_id": notification_id}


def reply(config, request):
    """Resolve dedicated-chat text, then reuse the versioned reply command."""
    if not config["messaging"].get("contextual_replies"):
        return {"ok": True, "status": "ignored"}
    try:
        identity = authenticate(config, request)
    except WorkItemValidationError:
        # The global OpenClaw dispatch hook must leave unrelated chats alone.
        return {"ok": True, "status": "ignored"}
    message_id = validate_text(request.get("message_id"), "message_id", 256, required=True)
    body = validate_text(request.get("body"), "body", MAX_REPLY, required=True)
    safe = redact_text(body)
    if not safe["ok"]:
        raise WorkItemValidationError("Response contains sensitive text; use local controls.")
    body = safe["text"]
    quote_body = validate_text(request.get("quote_body", ""), "quote_body", 20000)
    quote_message_id = validate_text(request.get("quote_message_id", ""), "quote_message_id", 256)
    quoted = request.get("quoted", False)
    if type(quoted) is not bool:
        raise WorkItemValidationError("quoted must be a boolean")
    key = (
        "chat-reply:"
        + hashlib.sha256(
            json.dumps([identity, request["conversation"], request["thread"], message_id]).encode()
        ).hexdigest()
    )
    with task_publication_lock(config["db_path"], key) as acquired:
        if not acquired:
            raise WorkItemConflictError("This reply is being processed. Resend it after completion.")
        with _connection(config) as conn:
            allowed = messages.allowed_repo_ids(conn, config)
            receipt = records.first(work_item_events.find_chat_reply_receipt(conn, key, identity, allowed))
            if receipt:
                if receipt[0] != body:
                    raise WorkItemConflictError("This incoming message already contains a different reply.")
                result = json.loads(receipt[1])["command_result"]
                return {
                    "ok": True,
                    "status": "accepted",
                    "notification_id": receipt[2],
                    "task_id": result.get("task_id"),
                    "version": result["item"]["version"],
                }
            commands_in_quote = re.findall(r"/robert-respond\s+(\S+)\s+(\d+)\s+reply\b", quote_body)
            delivery_ids = re.findall(r"(?:^|\n)Delivery:\s*(\S+)", quote_body)
            ids = {identifier for identifier, _ in commands_in_quote}
            ids.update(value.rsplit(":", 1)[0] for value in delivery_ids)
            if len(ids) > 1:
                raise WorkItemValidationError("The quote contains multiple Robert questions. Quote one message.")
            if (quoted or quote_body or quote_message_id) and not ids and not quote_message_id:
                raise WorkItemValidationError("The quoted question cannot be identified. Use /robert-inbox.")
            selected = notifications.find_chat_question(
                conn,
                messaging_repo_ids=allowed,
                messaging_settings=config["messaging"],
                notification_id=next(iter(ids), None),
                message_id=(quote_message_id or None) if not ids else None,
            )
            if not selected:
                if quoted or quote_body or quote_message_id:
                    raise WorkItemValidationError("The quoted question is unavailable. Use /robert-inbox.")
                return {"ok": True, "status": "no_question"}
            if len(selected) != 1:
                raise WorkItemValidationError("The quote matches multiple questions. Use /robert-inbox.")
            notification_id, raw, status = selected[0]
            delivery = json.loads(raw)["delivery"]
            if ids and quote_message_id and delivery.get("message_id") and quote_message_id != delivery["message_id"]:
                raise WorkItemValidationError("The quote identifiers do not match. Use /robert-inbox.")
            if any(value != delivery["delivery_id"] for value in delivery_ids):
                raise WorkItemConflictError("The quoted delivery changed. Use /robert-inbox.")
            version = delivery.get("version")
            if type(version) is not int or any(int(value) != version for _, value in commands_in_quote):
                raise WorkItemConflictError("The quoted question version changed. Use /robert-inbox.")
            envelope = messages.envelope(conn, notification_id, config=config)
            if status == "resolved" or not envelope:
                raise WorkItemConflictError("The quoted question was already handled. Use /robert-inbox.")
            if "reply" not in envelope["actions"]:
                raise WorkItemValidationError("This question does not accept a text reply. Use /robert-inbox.")
        return respond(
            config,
            {**request, "notification_id": notification_id, "version": version, "action": "reply", "body": body},
            idempotency_key=key,
        )

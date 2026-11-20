"""Attempt-scoped, paginated public Agent conversation records."""

import base64
from contextlib import closing
import json
import os
from pathlib import Path

from robert_agent.adapters.processes import session_history as history
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.foundation.values import json_object
from robert_agent.persistence import database, records
from robert_agent.persistence.queries import agent_sessions, artifacts, attempts
from robert_agent.security.redaction import redact_and_truncate


class SessionQueryError(ValueError):
    pass


def _safe(text):
    result = redact_and_truncate(str(text or ""), 16000, strict_assignments=True)
    return result.get("text", "[Sensitive content omitted]"), result.get("truncated", False)


def _record(item, timestamp, offset):
    kind = item.get("type")
    if not isinstance(kind, str):
        return None
    normalized = {
        "AgentMessage": "assistant",
        "agent_message": "assistant",
        "UserMessage": "user",
        "Reasoning": "reasoning",
        "reasoning": "reasoning",
        "CommandExecution": "command",
        "command_execution": "command",
        "FileChange": "file_change",
        "file_change": "file_change",
        "WebSearch": "web_search",
        "web_search": "web_search",
        "McpToolCall": "tool",
        "mcp_tool_call": "tool",
        "tool_call": "tool",
        "tool_output": "tool_output",
    }.get(kind)
    if not normalized:
        return None
    text = ""
    output = ""
    if normalized in {"assistant", "user"}:
        text = item.get("text") or history.text_content(item.get("content"))
    elif normalized == "reasoning":
        # Only the provider's public summary. Never raw_content or encrypted_content.
        text = item.get("text") if kind == "reasoning" else history.text_content(item.get("summary_text"))
        if not text:
            return None
    elif normalized == "command":
        command = item.get("command", "")
        text = " ".join(str(part) for part in command) if isinstance(command, list) else str(command)
        output = item.get("aggregated_output", "")
    elif normalized == "file_change":
        changes = item.get("changes", {})
        if isinstance(changes, dict):
            text = "\n".join(
                f"{change.get('type', 'update') if isinstance(change, dict) else 'update'} · {path}"
                for path, change in changes.items()
            )
            output = "\n".join(
                str(change.get("unified_diff", "")) for change in changes.values() if isinstance(change, dict)
            )
        elif isinstance(changes, list):
            text = "\n".join(
                f"{change.get('kind', 'update')} · {change.get('path', '')}"
                for change in changes
                if isinstance(change, dict)
            )
    elif normalized == "web_search":
        text = item.get("query") or json.dumps(item.get("action", {}), ensure_ascii=False)
    elif normalized == "tool":
        text = item.get("name") or item.get("tool") or "Tool"
        output = item.get("input") or item.get("arguments") or item.get("result") or ""
    else:
        text = "Tool result"
        output = history.text_content(item.get("output"))
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    text, text_truncated = _safe(text)
    output, output_truncated = _safe(output)
    return {
        "id": str(item.get("id") or f"record-{offset}"),
        "kind": normalized,
        "timestamp": timestamp if isinstance(timestamp, str) and len(timestamp) < 64 else None,
        "text": text,
        "output": output,
        "status": item.get("status")
        if isinstance(item.get("status"), str) and item["status"] in {"completed", "failed", "in_progress", "declined"}
        else "",
        "exit_code": item.get("exit_code") if type(item.get("exit_code")) is int else None,
        "truncated": text_truncated or output_truncated,
    }


def _event_record(event, source, modern, offset):
    if source == "stdout":
        if event.get("type") not in ("item.completed", "item.started"):
            return None
        item = event.get("item")
    else:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            return None
        if event.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
        elif not modern and event.get("type") == "response_item":
            if payload.get("type") == "message" and payload.get("role") in ("assistant", "user"):
                item = {**payload, "type": "AgentMessage" if payload["role"] == "assistant" else "UserMessage"}
            elif payload.get("type") == "reasoning":
                item = {"type": "Reasoning", "summary_text": payload.get("summary", [])}
            elif payload.get("type") in ("function_call", "custom_tool_call"):
                item = {**payload, "type": "tool_call"}
            elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
                item = {**payload, "type": "tool_output"}
            else:
                return None
        else:
            return None
    return _record(item, event.get("timestamp"), offset) if isinstance(item, dict) else None


def _decode_cursor(cursor):
    if not cursor:
        return None
    try:
        if len(cursor) > 1024:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, dict) or value["source"] not in {"session", "stdout"}:
            raise ValueError
        if not all(type(value[key]) is int and value[key] >= 0 for key in ("dev", "ino", "offset")):
            raise ValueError
        if value["source"] == "session" and not history.valid_session_id(value["session_id"]):
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise SessionQueryError("Invalid session cursor; refresh the session.") from exc


def load_agent_session(db_path, task_id, attempt_id, *, limit=30, cursor=None, run_id=None):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise SessionQueryError("limit must be between 1 and 50")
    position = _decode_cursor(cursor)
    if position and (position.get("task_id"), position.get("attempt_id"), position.get("run_id")) != (
        task_id,
        attempt_id,
        run_id,
    ):
        raise SessionQueryError("Cursor belongs to another execution; refresh the session.")
    with closing(database.connect(db_path)) as conn:
        attempt = records.first(attempts.find_for_build_attempt(conn, attempt_id))
        if not attempt or attempt["task_id"] != task_id:
            return None
        attempt = dict(attempt)
        registered = [
            dict(row)
            for row in artifacts.find_artifact_id_and_artifact_type_and_path_and_bytes_by_attempt_id(conn, attempt_id)
        ]
        runs = [dict(row) for row in agent_sessions.model_runs_for_attempt(conn, task_id, attempt_id)]
    metadata = json_object(attempt["metadata_json"])
    dispatch = metadata.get("dispatch", {})
    agent = dispatch.get("worker_agent") or dispatch.get("worker_launch", {}).get("agent")
    root = Path(db_path).expanduser().absolute().parent
    stdout = next((row["path"] for row in registered if row["artifact_type"] == "worker_stdout"), None)
    started_at = utc_timestamp(attempt["started_at"])
    active = attempt["status"] == "running"
    prompt_sha256 = None
    if run_id:
        run = next((row for row in runs if row["run_id"] == run_id), None)
        if not run:
            return None
        dispatch = json_object(run["dispatch_json"])
        stdout = dispatch.get("stdout_path")
        agent = dispatch.get("worker_agent") or dispatch.get("worker_launch", {}).get("agent")
        started_at = utc_timestamp(run["started_at"] or run["created_at"])
        active = run["status"] in {"running", "starting"}
        prompt_sha256 = run["prompt_sha256"]
    registered_sid = history.stdout_session_id(root, stdout)
    sid = position.get("session_id") if position else registered_sid
    if position and run_id and registered_sid != sid:
        raise SessionQueryError("Session history changed; refresh the session.")
    native = (
        None
        if (position and position["source"] == "stdout") or (run_id and not registered_sid)
        else history.find_native_history(
            agent,
            started_at,
            task_id,
            attempt_id,
            sid,
            prompt_sha256=prompt_sha256,
        )
    )
    source = "session" if native else "stdout"
    path = native["path"] if native else stdout
    result = {
        "ok": True,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "session_id": native["session_id"] if native else sid,
        "source": source,
        "active": active,
        "run_id": run_id,
        "runs": [
            {
                "run_id": row["run_id"],
                "stage": row["stage"],
                "worker": _safe(row["worker_id"])[0],
                "status": row["status"],
            }
            for row in runs
        ],
        "items": [],
        "next_cursor": None,
        "status": "unavailable",
        "limited": False,
    }
    if not path or (position and source != position["source"]):
        if position:
            raise SessionQueryError("Session history changed; refresh the session.")
        return result
    try:
        with history.open_history(native["root"] if native else root, path) as fh:
            info = os.fstat(fh.fileno())
            if position and (
                position["dev"] != info.st_dev or position["ino"] != info.st_ino or position["offset"] > info.st_size
            ):
                raise SessionQueryError("Session history changed; refresh the session.")
            if (
                native
                and not history.session_binding(
                    fh, task_id, attempt_id, native["session_id"], prompt_sha256=prompt_sha256
                )[0]
            ):
                return result
            end = position["offset"] if position else history.complete_end(fh, info.st_size)
            seen = set()
            next_offset = 0
            line = b""
            offset = 0
            for offset, line in history.reverse_lines(fh, end):
                if line is None:
                    result["limited"] = True
                    next_offset = offset
                    continue
                record = _event_record(history.json_object(line), source, bool(native and native["modern"]), offset)
                if not record or record["id"] in seen:
                    continue
                seen.add(record["id"])
                result["items"].append(record)
                next_offset = offset
                if len(result["items"]) >= limit:
                    break
            else:
                next_offset = offset if line is None else 0
            if next_offset > 0:
                value = {
                    "source": source,
                    "session_id": result["session_id"],
                    "dev": info.st_dev,
                    "ino": info.st_ino,
                    "offset": next_offset,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                }
                result["next_cursor"] = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
            result["status"] = "ready" if result["items"] else "empty"
    except (OSError, ValueError) as exc:
        if isinstance(exc, SessionQueryError):
            raise
        if position:
            raise SessionQueryError("Session history unavailable; refresh the session.") from exc
    return result

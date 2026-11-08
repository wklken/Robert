"""Bounded external transports for the versioned operator message envelope."""

import json
import os
import shlex
import subprocess


def configured(config=None):
    return bool(os.environ.get("ROBERT_NOTIFICATION_COMMAND") or (config or {}).get("messaging", {}).get("enabled"))


def render_message(payload):
    lines = ["Robert: " + payload["type"]]
    for key in (
        "title",
        "summary",
        "issue_url",
        "pr_url",
        "result_url",
        "reason_code",
        "safe_error",
        "required_action",
    ):
        if payload.get(key):
            lines.append(str(payload[key]))
    for choice in payload.get("choices", []):
        lines.append(f"{choice['id']}: {choice['label']}")
    if payload.get("task_id"):
        lines.append("Task: " + payload["task_id"])
    if payload.get("request_id"):
        lines.append("/robert-inbox status " + payload["request_id"])
    if payload.get("notification_id") and payload.get("actions", ["ack"]):
        lines.append("Response commands:")
        for action in payload.get("actions", ["ack"]):
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

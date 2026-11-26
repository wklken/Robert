#!/usr/bin/env python3
"""Call a local model gateway without exposing tools."""

from robert_agent.adapters.processes import model_usage
from robert_agent.adapters.services import tcodex_gateway

import argparse
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from robert_agent.foundation import question_choices
from robert_agent.foundation.contracts.supervision import AUTOMATIC_DECISION_ACTIONS, NONEXECUTING_DECISION_ACTIONS

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 1048576
MAX_OUTPUT_BYTES = 8192
MAX_METADATA_BYTES = 8192


class ModelError(ValueError):
    """A fixed diagnostic that contains no model, evidence or credential text."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_json(value):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ModelError("duplicate JSON field")
        return result

    try:
        return json.loads(value, object_pairs_hook=unique)
    except RecursionError:
        raise ModelError("model JSON is nested too deeply") from None


def checked_endpoint(value):
    if not isinstance(value, str):
        raise ModelError("model endpoint must be the local gateway")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ModelError("model endpoint must be the local gateway")
    return f"http://127.0.0.1:{parsed.port}"


def load_gateway(metadata):
    try:
        fd = os.open(metadata, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ModelError("untrusted gateway metadata") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid != os.geteuid():
            raise ModelError("untrusted gateway metadata")
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ModelError("untrusted gateway metadata")
    value = load_json(raw)
    if isinstance(value, dict) and value.get("type") == "tcodex":
        paths = (value.get("executable"), value.get("home"))
        if not all(isinstance(path, str) and os.path.isabs(path) and "\0" not in path for path in paths):
            raise ModelError("tcodex gateway requires absolute executable and home paths")
        return tcodex_gateway.TcodexGateway(*paths)
    return checked_endpoint(value.get("url") if isinstance(value, dict) else None)


def validate_result(result, bundle, kind):
    base = {"bundle_id", "bundle_sha256"}
    fields = (
        base
        | {
            "action",
            "candidate_id",
            "reason",
            "evidence_ids",
            "expected_progress",
            "recheck_seconds",
            "message",
        }
        if kind == "decision"
        else base | {"diagnosis_class", "evidence_ids", "recommended_action", "expected_progress"}
    )
    if kind == "decision" and isinstance(result, dict) and result.get("action") == "ask_user" and "choices" in result:
        fields.add("choices")
    if not isinstance(result, dict) or set(result) != fields:
        raise ModelError("invalid model output fields")
    if any(result[name] != bundle[name] for name in base):
        raise ModelError("model output does not match the evidence bundle")
    evidence = result["evidence_ids"]
    known = {item["id"] for item in bundle["evidence"]}
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(item, str) or item not in known for item in evidence)
        or len(evidence) != len(set(evidence))
    ):
        raise ModelError("model output references unknown evidence")
    if kind == "decision":
        action, candidate = result["action"], result["candidate_id"]
        if not isinstance(action, str):
            raise ModelError("invalid model action")
        if action in AUTOMATIC_DECISION_ACTIONS:
            if not any(item["id"] == candidate and item["action"] == action for item in bundle["candidates"]):
                raise ModelError("model selected an unavailable candidate")
        elif action not in NONEXECUTING_DECISION_ACTIONS or action not in bundle["allowed_actions"]:
            raise ModelError("model selected an unavailable action")
        elif candidate is not None:
            raise ModelError("non-execution decision cannot target a candidate")
        if type(result["recheck_seconds"]) is not int or not 30 <= result["recheck_seconds"] <= 3600:
            raise ModelError("invalid model recheck interval")
        for name in ["reason", "expected_progress", "message"]:
            if not isinstance(result[name], str) or len(result[name]) > 2000:
                raise ModelError("invalid model text")
        if not result["reason"].strip() or not result["expected_progress"].strip():
            raise ModelError("model output needs an observable reason and progress")
        if action in ["ask_user", "notify_user"] and not result["message"].strip():
            raise ModelError("model output needs an operator message")
        if action == "ask_user":
            error = question_choices.validation_error(
                result.get("choices", []), required=bundle.get(question_choices.REQUIRED_FLAG, False)
            )
            if error:
                raise ModelError("invalid model question choices")
    else:
        if result["diagnosis_class"] not in [
            "transient_failure",
            "expected_wait",
            "repeated_failure",
            "goal_deviation",
            "product_defect",
            "operator_needed",
            "unknown",
        ]:
            raise ModelError("invalid diagnosis class")
        if result["recommended_action"] not in [
            "wait",
            "reconcile",
            "repair_task",
            "ask_user",
        ]:
            raise ModelError("invalid diagnosis recommendation")
        if (
            not isinstance(result["expected_progress"], str)
            or not result["expected_progress"].strip()
            or len(result["expected_progress"]) > 1000
        ):
            raise ModelError("invalid diagnosis progress")
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        raise ModelError("model output exceeds the protocol limit")
    return result


def send_request(request, timeout):
    return build_opener(ProxyHandler({}), NoRedirects()).open(request, timeout=timeout)


def retry_seconds(value):
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            seconds = int((instant - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, min(86400, seconds))


class RateLimited(ModelError):
    def __init__(self, provider_error):
        super().__init__("local model gateway rate limited the request")
        self.provider_error = provider_error


def _read_gateway_response(request, timeout):
    try:
        with send_request(request, timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        try:
            if error.code != 429:
                raise ModelError("local model gateway returned HTTP " + str(error.code)) from None
            provider_error = {"code": "rate_limited"}
            delay = retry_seconds(error.headers.get("Retry-After"))
            if delay is not None:
                provider_error["retry_after_seconds"] = delay
            raise RateLimited(provider_error) from None
        finally:
            error.close()
    except HTTPException:
        raise ModelError("local model gateway returned an invalid HTTP response") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelError("model response exceeds the transport limit")
    return raw


def _messages_request(endpoint, model, instructions, evidence, timeout, session_id, on_usage):
    payload = {
        "model": model,
        "max_tokens": 4096,
        "thinking": {"type": "disabled"},
        "tools": [],
        "tool_choice": {"type": "none"},
        "system": instructions,
        "messages": [{"role": "user", "content": evidence}],
    }
    request = Request(
        checked_endpoint(endpoint) + "/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "X-Claude-Code-Session-Id": session_id,
        },
    )
    response = load_json(_read_gateway_response(request, timeout))
    if on_usage and isinstance(response, dict):
        on_usage(model_usage.gateway_usage(response))
    if not isinstance(response, dict) or response.get("stop_reason") != "end_turn":
        raise ModelError("model response did not complete without tools")
    blocks = response.get("content")
    if not isinstance(blocks, list):
        raise ModelError("model response contains invalid content")
    text = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ModelError("model response contains an invalid content block")
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block_type == "thinking" and (
            isinstance(block.get("thinking"), str)
            or ("thinking" not in block and isinstance(block.get("signature"), str))
        ):
            continue
        elif block_type == "redacted_thinking" and isinstance(block.get("data"), str):
            continue
        else:
            raise ModelError("model response contains an unsupported or malformed content block")
    return text


def _completed_response(raw):
    completed = None
    for event in raw.replace(b"\r\n", b"\n").split(b"\n\n"):
        payload = b"\n".join(line[5:].removeprefix(b" ") for line in event.split(b"\n") if line.startswith(b"data:"))
        if not payload or payload == b"[DONE]":
            continue
        item = load_json(payload)
        if not isinstance(item, dict) or item.get("type") in ("response.failed", "response.incomplete", "error"):
            raise ModelError("model response did not complete without tools")
        if item.get("type") == "response.completed":
            if completed is not None:
                raise ModelError("model response contains multiple completions")
            completed = item.get("response")
            if not isinstance(completed, dict) or completed.get("status") != "completed":
                raise ModelError("model response did not complete without tools")
    if completed is None:
        raise ModelError("model response did not complete without tools")
    return completed


def _responses_text(response):
    text = []
    output = response.get("output")
    if not isinstance(output, list):
        raise ModelError("model response contains invalid content")
    for item in output:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            continue
        if (
            not isinstance(item, dict)
            or item.get("type") != "message"
            or item.get("role") != "assistant"
            or not isinstance(item.get("content"), list)
        ):
            raise ModelError("model response contains an unsupported or malformed output item")
        for block in item["content"]:
            if (
                not isinstance(block, dict)
                or block.get("type") != "output_text"
                or not isinstance(block.get("text"), str)
            ):
                raise ModelError("model response contains an unsupported or malformed content block")
            text.append(block["text"])
    return text


def _responses_request(gateway, model, instructions, evidence, timeout, session_id, on_usage):
    payload = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": evidence}]}],
        "max_output_tokens": 4096,
        "reasoning": {"effort": "low"},
        "tools": [],
        "tool_choice": "none",
        "store": False,
        "stream": True,
    }
    deadline = time.monotonic() + timeout
    try:
        with tcodex_gateway.open_gateway(gateway, deadline) as endpoint:
            request = Request(
                checked_endpoint(endpoint) + "/responses",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Session_ID": session_id,
                    "X-Conversation-Request-ID": str(uuid4()),
                },
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelError("tcodex gateway startup timed out")
            response = _completed_response(_read_gateway_response(request, remaining))
    except tcodex_gateway.GatewayStartupError as error:
        raise ModelError(str(error)) from None
    if on_usage:
        on_usage(model_usage.gateway_usage(response))
    return _responses_text(response)


def request_json(*, instructions, evidence, endpoint, model, timeout, session_id, on_usage=None):
    """Return a JSON result through a trusted local gateway."""
    serialized = json.dumps(evidence, ensure_ascii=False)
    if len(serialized.encode()) > MAX_REQUEST_BYTES:
        raise ModelError("model input exceeds the transport limit")
    request = _responses_request if isinstance(endpoint, tcodex_gateway.TcodexGateway) else _messages_request
    text = "".join(request(endpoint, model, instructions, serialized, timeout, session_id, on_usage))
    if not text.strip():
        raise ModelError("model response contains no decision text")
    return load_json(text)


def call(bundle, kind, endpoint, model, timeout, *, on_usage=None):
    if (
        not isinstance(bundle, dict)
        or not all(name in bundle for name in ["bundle_id", "bundle_sha256", "instructions", "evidence"])
        or not isinstance(bundle["instructions"], str)
    ):
        raise ModelError("invalid evidence bundle")
    if kind == "decision" and not all(name in bundle for name in ["allowed_actions", "candidates"]):
        raise ModelError("decision bundle is missing its action policy")

    try:
        result = request_json(
            instructions=bundle["instructions"],
            evidence=bundle,
            endpoint=endpoint,
            model=model,
            timeout=timeout,
            session_id="robert-supervision-" + bundle["bundle_id"],
            on_usage=on_usage,
        )
    except RateLimited as error:
        return {
            "bundle_id": bundle["bundle_id"],
            "bundle_sha256": bundle["bundle_sha256"],
            "error": error.provider_error,
        }
    return validate_result(result, bundle, kind)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["decision", "diagnosis"], required=True)
    parser.add_argument("--gateway-metadata", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ModelError("evidence bundle exceeds the input limit")
        bundle = load_json(raw)
        result = call(
            bundle,
            args.kind,
            load_gateway(args.gateway_metadata),
            args.model,
            args.timeout,
            on_usage=model_usage.report_sidecar,
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except ModelError as error:
        print("Robert supervision model unavailable: " + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, URLError) as error:
        # Raw response bodies, evidence and credentials never enter stderr.
        print(
            "Robert supervision model unavailable (" + type(error).__name__ + ")",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

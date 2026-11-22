"""Bounded consumption of metering emitted by local model adapters."""

import os
import json
from pathlib import Path
import sys

from robert_agent.adapters.processes import session_history


SIDECAR_ENV = "ROBERT_MODEL_USAGE_FILE"
SIDECAR_NAME = "model-usage.json"
MAX_SIDECAR_BYTES = 4096
_TOKEN_KEYS = (
    "input_tokens",
    "inputTokens",
    "output_tokens",
    "outputTokens",
    "cache_read_input_tokens",
    "cacheReadInputTokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cacheCreationInputTokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
)


def _tokens(value):
    return (
        {key: value[key] for key in _TOKEN_KEYS if key in value and type(value[key]) is int and value[key] >= 0}
        if isinstance(value, dict)
        else {}
    )


def _usd(value):
    return value if type(value) in (int, float) and 0 <= value <= sys.float_info.max else None


def gateway_usage(response):
    """Keep only measurements from the response envelope outside generated content."""
    counters = _tokens(response.get("usage"))
    cost = _usd(response.get("total_cost_usd"))
    if not counters and cost is None:
        return {}
    return {
        "source": "gateway",
        "usage_available": True,
        "usage": counters,
        "total_cost_usd": cost,
    }


def read_sidecar(root, path=None):
    """Optional numeric report written by a configured command in its private directory."""
    try:
        with session_history.open_history(root, path or Path(root) / SIDECAR_NAME, trusted_root_alias=True) as stream:
            raw = stream.read(MAX_SIDECAR_BYTES + 1)
    except (OSError, ValueError):
        return {}
    return gateway_usage(session_history.json_object(raw)) if len(raw) <= MAX_SIDECAR_BYTES else {}


def report_sidecar(usage):
    """Relay trusted transport measurements to the parent, if it asked for them."""
    path = os.environ.get(SIDECAR_ENV)
    if path and usage:
        try:
            with open(path, "x", encoding="utf-8") as stream:
                json.dump(usage, stream, allow_nan=False)
        except OSError:
            pass


def event_usage(event):
    if not event:
        return {"usage_available": False, "source": "no_result_event"}
    provider = event.get("providerData")
    provider = provider if isinstance(provider, dict) else {}
    counters = _tokens(event.get("usage"))
    usage = _tokens(provider.get("usage"))
    raw = _tokens(provider.get("rawUsage"))
    cost = _usd(event.get("total_cost_usd"))
    return {
        "usage_available": bool(counters or usage or raw or cost is not None),
        "source": "codex_json" if event.get("type") == "turn.completed" else "cbc_stream_json",
        "usage": counters,
        "provider_usage": usage,
        "provider_raw_usage": raw,
        "total_cost_usd": cost,
        **{key: event[key] for key in ("duration_ms", "num_turns") if type(event.get(key)) is int},
    }


def _opencode(events):
    totals = {}
    seen = set()
    partial = False
    partial_cost = False
    cost = None
    for event in events:
        if event.get("type") != "step_finish" or not isinstance(event.get("part"), dict):
            continue
        part = event["part"]
        key = part.get("id")
        if not isinstance(key, str) or key in seen:
            continue
        seen.add(key)
        tokens = part.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, dict) else {}
        counters = _tokens(
            {
                "input_tokens": tokens.get("input"),
                "output_tokens": tokens.get("output"),
                "cache_read_input_tokens": cache.get("read"),
                "cache_creation_input_tokens": cache.get("write"),
            }
        )
        partial |= len(counters) != 4
        reasoning = tokens.get("reasoning")
        if "output_tokens" in counters and type(reasoning) is int and 0 <= reasoning <= sys.maxsize:
            # OpenCode reports visible output and billed reasoning separately.
            counters["output_tokens"] += reasoning
        for name, value in counters.items():
            totals[name] = totals.get(name, 0) + value
        step_cost = _usd(part.get("cost"))
        if step_cost is not None and step_cost <= sys.float_info.max - (cost or 0):
            cost = (cost or 0) + step_cost
        else:
            partial_cost = True
    if not totals and cost is None:
        return {}
    return {
        "source": "opencode",
        "usage_available": True,
        "usage": totals,
        "total_cost_usd": cost,
        "partial": partial,
        "partial_cost": partial_cost,
    }


def read_log(root, path, *, agent=None, complete_lines=True):
    """Read only registered stdout under the runtime root."""
    if not isinstance(path, str) or not path:
        return {}
    try:
        with session_history.open_history(root, path, trusted_root_alias=True) as stream:
            size = os.fstat(stream.fileno()).st_size
            end = session_history.complete_end(stream, size) if complete_lines else size
            lines = session_history.reverse_lines(stream, end)
            if agent == "opencode":
                events = (session_history.json_object(line) for _, line in lines if line is not None)
                result = _opencode(events)
                if result and end > session_history.PAGE_SCAN_BYTES:
                    result.update(partial=True, partial_cost=True)
                return result
            for _, line in lines:
                if line is None:
                    continue
                event = session_history.json_object(line)
                if event.get("type") in {"result", "turn.completed"}:
                    return event_usage(event)
    except (OSError, ValueError):
        pass
    return {}


def read_legacy_log(path):
    """Read a persisted stdout path used before dispatch registered its adapter."""
    if not isinstance(path, str) or not path:
        return {}
    return read_log(Path(path).parent, path, complete_lines=False)


def dispatch_sidecar(stdout):
    return Path(stdout).with_suffix(".usage.json")


def read_dispatch(root, dispatch):
    path = dispatch.get("stdout_path")
    if dispatch.get("worker_agent") == "command" and isinstance(path, str) and path:
        if measured := read_sidecar(root, dispatch_sidecar(path)):
            return measured
    return read_log(root, path, agent=dispatch.get("worker_agent"))

"""Bounded consumption of metering emitted by local model adapters."""

import os
import json
from pathlib import Path
import sys

from robert_agent.adapters.processes import session_history
from robert_agent.foundation.timestamps import utc_timestamp


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
_ALIASES = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cached_input_tokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "cache_write_input_tokens": "cache_creation_input_tokens",
}


def _tokens(value):
    if not isinstance(value, dict):
        return {}
    counters = {}
    for key in _TOKEN_KEYS:
        count = value.get(key)
        if type(count) is int and count >= 0:
            counters.setdefault(_ALIASES.get(key, key), count)
    return counters


def _usd(value):
    return value if type(value) in (int, float) and 0 <= value <= sys.float_info.max else None


def source_for_agent(agent, event_type=None):
    """Use the registered adapter, retaining labels for old unregistered logs."""
    if agent in {"claude", "tclaude"}:
        return "claude_stream_json"
    if agent in {"codex", "tcodex"} or event_type == "turn.completed":
        return "codex_json"
    if agent in {"opencode", "command"}:
        return "opencode" if agent == "opencode" else "gateway"
    return "cbc_stream_json" if agent in {None, "cbc"} else agent


def gateway_usage(response):
    """Keep only measurements from the response envelope outside generated content."""
    usage = response.get("usage")
    counters = _tokens(usage)
    if isinstance(usage, dict):
        for detail, source_key, target_key in (
            ("input_tokens_details", "cached_tokens", "cache_read_input_tokens"),
            ("output_tokens_details", "reasoning_tokens", "reasoning_output_tokens"),
        ):
            details = usage.get(detail)
            count = details.get(source_key) if isinstance(details, dict) else None
            if type(count) is int and count >= 0:
                counters.setdefault(target_key, count)
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


def event_usage(event, *, agent=None):
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
        "source": source_for_agent(agent, event.get("type")),
        "usage": counters,
        "provider_usage": usage,
        "provider_raw_usage": raw,
        "total_cost_usd": cost,
        **{key: event[key] for key in ("duration_ms", "num_turns") if type(event.get(key)) is int},
    }


def _add_blocks(target, incoming):
    for block in ("usage", "provider_usage", "provider_raw_usage"):
        for key, value in incoming.get(block, {}).items():
            target.setdefault(block, {})[key] = target.get(block, {}).get(key, 0) + value
    cost = _usd(incoming.get("total_cost_usd"))
    if cost is not None and cost <= sys.float_info.max - (target.get("total_cost_usd") or 0):
        target["total_cost_usd"] = (target.get("total_cost_usd") or 0) + cost


def _cumulative(events, baseline):
    """Convert ordered scope snapshots into increments, including explicit resets.

    Reset detection belongs to the provider stream normalization, not the
    durable upsert, which only merges already normalized receipt contributions.
    """
    totals, previous, resets = {}, baseline or {}, 0
    for incoming in events:
        delta = {}
        reset = any(
            value < previous.get(block, {}).get(key, 0)
            for block in ("usage", "provider_usage", "provider_raw_usage")
            for key, value in incoming.get(block, {}).items()
        ) or (
            _usd(incoming.get("total_cost_usd")) is not None
            and _usd(previous.get("total_cost_usd")) is not None
            and incoming["total_cost_usd"] < previous["total_cost_usd"]
        )
        if reset:
            previous = {}
            resets += 1
        for block in ("usage", "provider_usage", "provider_raw_usage"):
            for key, value in incoming.get(block, {}).items():
                delta.setdefault(block, {})[key] = max(0, value - previous.get(block, {}).get(key, 0))
        cost = _usd(incoming.get("total_cost_usd"))
        if cost is not None:
            delta["total_cost_usd"] = max(0, cost - (_usd(previous.get("total_cost_usd")) or 0))
        _add_blocks(totals, delta)
        previous = {**previous, **incoming}
    return totals, resets


def _stream_usage(events, *, agent, baseline=None, counter_scope=None, truncated=False):
    turns, snapshots, messages, results = [], [], {}, []
    seen = set()
    source_offsets = []
    receipt_ids = []
    open_turn = False
    completed_at = None
    for offset, event in events:
        event_type = event.get("type")
        if event_type in {"turn.started", "turn.failed"}:
            open_turn = True
        if event_type in {"turn.completed", "result"}:
            receipt = event_usage(event, agent=agent)
            if not receipt.get("usage_available"):
                continue
            identity = event.get("turn_id") or event.get("uuid") or event.get("id")
            key = (event_type, identity) if isinstance(identity, str) else (event_type, offset)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(identity, str):
                receipt_ids.append({"kind": event_type, "id": identity})
            open_turn = False
            stamp = event.get("timestamp")
            completed_at = stamp if utc_timestamp(stamp) else None
            (turns if event_type == "turn.completed" else results).append(receipt)
            source_offsets.append(offset)
        elif agent in {"codex", "tcodex"} and event_type == "event_msg":
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "task_started":
                open_turn = True
                completed_at = None
            elif payload.get("type") == "task_complete":
                open_turn = False
                stamp = event.get("timestamp")
                completed_at = stamp if utc_timestamp(stamp) else None
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            counters = _tokens(info.get("total_token_usage")) if isinstance(info, dict) else {}
            if counters:
                snapshots.append({"usage": counters})
                source_offsets.append(offset)
        elif agent in {"claude", "tclaude"} and event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                continue
            counters = _tokens(message.get("usage"))
            if counters:
                # Claude can emit updated envelopes for the same message.
                previous = messages.get(message["id"], {})
                messages[message["id"]] = {
                    key: max(previous.get(key, 0), value) for key, value in {**previous, **counters}.items()
                }
                source_offsets.append(offset)
    totals, resets = {}, 0
    if results:
        # result is authoritative launch cumulative, replacing assistant deltas.
        totals, resets = _cumulative(results, baseline if counter_scope == "session" else None)
        totals.update({key: results[-1][key] for key in ("duration_ms", "num_turns") if key in results[-1]})
        mode, scope, complete = "cumulative", "session" if counter_scope == "session" else "launch", True
    elif snapshots:
        totals, resets = _cumulative(snapshots, baseline)
        mode, scope, complete = "cumulative", "session", bool(turns or completed_at)
    elif turns:
        for turn in turns:
            _add_blocks(totals, turn)
        mode, scope, complete = "delta", "turn", True
    elif messages:
        for counters in messages.values():
            _add_blocks(totals, {"usage": counters})
        mode, scope, complete = "delta", "request", False
    else:
        return {}
    return {
        **totals,
        "usage_available": True,
        "source": source_for_agent(agent, "turn.completed" if turns or snapshots else None),
        "measurement_status": "complete" if complete and not truncated and not open_turn else "partial",
        "partial": not complete or truncated or open_turn,
        "partial_cost": truncated,
        "counter_mode": mode,
        "counter_scope": scope,
        "counter_resets": resets,
        "source_offsets": source_offsets,
        **({"receipt_ids": receipt_ids} if receipt_ids else {}),
        **({"baseline": baseline} if baseline and mode == "cumulative" and scope == "session" else {}),
        **({"unknown_reason": "bounded_log_scan"} if truncated else {}),
        **(
            {"events": {"model_completed": {"at": completed_at, "source": "provider", "precision": "reported"}}}
            if completed_at and not open_turn
            else {}
        ),
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


def read_log(root, path, *, agent=None, complete_lines=True, baseline=None, counter_scope=None):
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
                if result:
                    result["measurement_status"] = "partial" if result.get("partial") else "complete"
                return result
            events = [(offset, session_history.json_object(line)) for offset, line in lines if line is not None]
            return _stream_usage(
                reversed(events),
                agent=agent,
                baseline=baseline,
                counter_scope=counter_scope,
                truncated=end > session_history.PAGE_SCAN_BYTES,
            )
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


def _native_file(binding, stream):
    info = os.fstat(stream.fileno())
    if binding.get("history_identity") != {"dev": info.st_dev, "ino": info.st_ino}:
        raise ValueError("native usage history identity changed")
    return info.st_size


def native_resume_usage_cursor(binding):
    """Pin the exact stopped session boundary before a new continuation starts."""
    try:
        with session_history.open_history(binding["history_root"], binding["history_path"]) as stream:
            size = _native_file(binding, stream)
            if session_history.complete_end(stream, size) != size:
                return None
            baseline = None
            if binding["agent"] in {"codex", "tcodex"}:
                for _, line in session_history.reverse_lines(stream, size):
                    event = session_history.json_object(line) if line else {}
                    payload = event.get("payload") or {}
                    if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                        baseline = _tokens((payload.get("info") or {}).get("total_token_usage"))
                        if baseline:
                            break
                if baseline is None:
                    return None  # A whole-session counter has no trustworthy starting point.
            return {"offset": size, "baseline": {"usage": baseline} if baseline else None}
    except (OSError, TypeError, ValueError, KeyError, AttributeError):
        return None


def read_bound_resume_usage(dispatch):
    """Read only new native receipts from one stopped continuation, with no stdout addition."""
    binding, cursor = dispatch.get("native_session"), dispatch.get("native_usage_cursor")
    if not isinstance(binding, dict) or not isinstance(cursor, dict) or not dispatch.get("native_resume"):
        return {}
    try:
        with session_history.open_history(binding["history_root"], binding["history_path"]) as stream:
            size = _native_file(binding, stream)
            start = cursor["offset"]
            if type(start) is not int or not 0 <= start < size:
                return {}
            stream.seek(start)
            content = stream.read(session_history.PAGE_SCAN_BYTES)
            last = content.rfind(b"\n")
            if last < 0:
                return {}
            events = (
                (index, session_history.json_object(line)) for index, line in enumerate(content[:last].splitlines())
            )
            result = _stream_usage(
                events,
                agent=binding["agent"],
                baseline=cursor.get("baseline"),
                truncated=start + last + 1 < size,
            )
            if result:
                result["observation_source"] = "bound_native_resume_history"
            return result
    except (OSError, TypeError, KeyError, ValueError, AttributeError):
        return {}


def read_dispatch(root, dispatch):
    path = dispatch.get("stdout_path")
    if dispatch.get("worker_agent") == "command" and isinstance(path, str) and path:
        if measured := read_sidecar(root, dispatch_sidecar(path)):
            return measured
    return read_log(
        root,
        path,
        agent=dispatch.get("worker_agent"),
        baseline=dispatch.get("usage_baseline"),
        counter_scope=dispatch.get("usage_counter_scope"),
    )

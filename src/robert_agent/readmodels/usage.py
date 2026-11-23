"""Usage and acceptance metrics helpers for Robert worker runs."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import worker_results as db_read_worker_results

from robert_agent.foundation.values import json_object
from robert_agent.adapters.processes import model_usage
import math
import sys
from itertools import chain


def _as_number(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, float):
        return value if math.isfinite(value) and value >= 0 else default
    try:
        number = int(value)
        return number if number >= 0 else default
    except (TypeError, ValueError):
        return default


def _reported_tokens(usage, *keys):
    for key in keys:
        value = usage.get(key)
        if value is not None:
            return _as_number(value, None)
    return None


def _usage_value(usage, *keys):
    return _reported_tokens(usage, *keys) or 0


def _first_usage_block(usage_payload):
    for key in ["usage", "provider_usage", "provider_raw_usage"]:
        block = usage_payload.get(key)
        if isinstance(block, dict) and block:
            return block
    return {}


def extract_attempt_usage(attempt_metadata):
    if not isinstance(attempt_metadata, dict):
        return {"usage_available": False, "source": "missing_metadata"}
    usage_payload = attempt_metadata.get("usage")
    if isinstance(usage_payload, dict):
        return usage_payload
    dispatch = attempt_metadata.get("dispatch") or {}
    path = dispatch.get("stdout_path")
    return model_usage.read_legacy_log(path) or {"usage_available": False, "source": "missing"}


def _result_counts(conn):
    accepted = db_records.first(db_read_worker_results.find_for_result_counts(conn))[0]
    rejected = db_records.first(db_read_worker_results.find_for_summarize_database(conn))[0]
    return accepted, rejected


def _action_counts(conn):
    published = db_records.first(db_read_github_actions.find_for_action_counts_2(conn))[0]
    failed = db_records.first(db_read_github_actions.find_for_action_counts_3(conn))[0]
    deduplicated = db_records.first(db_read_github_actions.find_for_action_counts_4(conn))[0]
    return published, failed, deduplicated


def _add_usage_totals(totals, usage_payload):
    if not isinstance(usage_payload, dict) or not usage_payload.get("usage_available"):
        return False
    block = _first_usage_block(usage_payload)
    totals["total_input_tokens"] += _usage_value(block, "input_tokens", "inputTokens")
    totals["total_output_tokens"] += _usage_value(block, "output_tokens", "outputTokens")
    totals["total_cache_creation_input_tokens"] += _usage_value(
        block,
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cache_write_input_tokens",
    )
    totals["total_cache_read_input_tokens"] += _usage_value(
        block,
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cached_input_tokens",
    )
    cost = usage_payload.get("total_cost_usd")
    if type(cost) in (int, float) and 0 <= cost <= sys.float_info.max:
        totals["total_cost_usd"] += float(cost)
        totals["measured_cost_attempt_count"] += 1
    input_value = block.get("input_tokens", block.get("inputTokens"))
    output_value = block.get("output_tokens", block.get("outputTokens"))
    if _as_number(input_value, None) is not None and _as_number(output_value, None) is not None:
        totals["measured_token_attempt_count"] += 1
    return True


def summarize_usage_payloads(usage_payloads):
    totals = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_input_tokens": 0,
        "total_cache_read_input_tokens": 0,
        "total_cost_usd": 0.0,
        "measured_token_attempt_count": 0,
        "measured_cost_attempt_count": 0,
    }
    available = False
    for payload in usage_payloads:
        available = _add_usage_totals(totals, payload) or available
    return {
        "usage_available": available,
        "total_tokens": (totals["total_input_tokens"] + totals["total_output_tokens"]),
        **totals,
    }


def _launched(dispatch, started_at):
    return bool(started_at or dispatch.get("pid"))


def _attempt_usage_payloads(root, attempts, recorded):
    for attempt, model_runs in attempts:
        metadata = json_object(attempt["metadata_json"])
        dispatch = metadata.get("dispatch")
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        saved = recorded.get(("worker", attempt["attempt_id"])) or metadata.get("usage")
        saved = saved if isinstance(saved, dict) else {}
        coordinator = dispatch.get("model_coordinator", bool(model_runs) and not saved.get("usage_available"))
        if not coordinator and (saved.get("usage_available") or _launched(dispatch, attempt["started_at"])):
            # Native coordinators are subprocess wrappers. Their model runs are
            # counted below, so an unmetered wrapper adds no extra model call.
            yield (
                saved
                if saved.get("usage_available")
                else (model_usage.read_dispatch(root, dispatch) if attempt["finished_at"] else {})
            )
        for run in model_runs:
            dispatch = json_object(run["dispatch_json"])
            if _launched(dispatch, run["started_at"]):
                yield recorded.get(("stage", run["run_id"])) or model_usage.read_dispatch(root, dispatch)


def _receipt_tokens(payload):
    block = _first_usage_block(payload)
    inputs = _reported_tokens(block, "input_tokens", "inputTokens")
    outputs = _reported_tokens(block, "output_tokens", "outputTokens")
    cached = _reported_tokens(block, "cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens")
    created = _reported_tokens(
        block, "cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write_input_tokens"
    )
    uncached = None
    source = payload.get("source")
    if source in {"cbc_stream_json", "gateway", "opencode"} and inputs is not None:
        uncached = inputs
        # Claude-style reports list reads and writes outside regular input.
        inputs += (cached or 0) + (created or 0)
    elif source == "codex_json" and None not in (inputs, cached, created):
        # Codex input already includes both cached buckets.
        remaining = inputs - cached - created
        if remaining >= 0:
            uncached = remaining
    buckets = {
        "input_tokens": inputs,
        "uncached_input_tokens": uncached,
        "output_tokens": outputs,
        "cached_input_tokens": cached,
        "cache_creation_input_tokens": created,
    }
    # Budget enforcement still uses the exact integer from the worker. Keep
    # out-of-range counters out of the browser's numeric aggregate.
    return {name: value if value is None or value <= sys.maxsize else None for name, value in buckets.items()}


def summarize_receipt_usage(root, attempts, *, recorded=None):
    attempts = tuple(attempts)
    recorded = recorded or {}
    totals = {
        "scope": "request" if any(attempt["lineage"] for attempt, _ in attempts) else "task",
        "run_count": 0,
        "token_run_count": 0,
        "billing_token_run_count": 0,
        **_receipt_tokens({}),
        "cost_run_count": 0,
        "reported_cost_usd": None,
    }
    auxiliary = (payload for (kind, _), payload in recorded.items() if kind not in {"worker", "stage"})
    for payload in chain(_attempt_usage_payloads(root, attempts, recorded), auxiliary):
        totals["run_count"] += 1
        tokens = _receipt_tokens(payload)
        for bucket, count in tokens.items():
            if count is not None:
                totals[bucket] = (totals[bucket] or 0) + count
        if tokens["input_tokens"] is not None and tokens["output_tokens"] is not None:
            totals["token_run_count"] += 1
            if not payload.get("partial") and all(count is not None for count in tokens.values()):
                totals["billing_token_run_count"] += 1
        cost = payload.get("total_cost_usd")
        current_cost = totals["reported_cost_usd"] or 0.0
        if type(cost) in (int, float) and 0 <= cost <= sys.float_info.max - current_cost:
            if not payload.get("partial_cost"):
                totals["cost_run_count"] += 1
            totals["reported_cost_usd"] = current_cost + float(cost)
    return totals


def summarize_acceptance_metrics(conn):
    accepted_results, rejected_results = _result_counts(conn)
    published_actions, publish_failed_actions, deduplicated_actions = _action_counts(conn)
    payloads = []
    for row in db_read_worker_results.find_for_summarize_acceptance_metrics(conn):
        metadata = json_object(row[0])
        payloads.append(metadata.get("usage"))
    usage_summary = summarize_usage_payloads(payloads)
    result_total = accepted_results + rejected_results
    publish_total = published_actions + publish_failed_actions
    return {
        "accepted_results": accepted_results,
        "rejected_results": rejected_results,
        "published_actions": published_actions,
        "publish_failed_actions": publish_failed_actions,
        "deduplicated_actions": deduplicated_actions,
        "accepted_result_rate": accepted_results / result_total if result_total else 0.0,
        "publish_success_rate": published_actions / publish_total if publish_total else 0.0,
        **{key: value for key, value in usage_summary.items() if key != "total_tokens"},
    }

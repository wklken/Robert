"""Usage and acceptance metrics helpers for Robert worker runs."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import worker_results as db_read_worker_results

import json
from pathlib import Path


def _as_number(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_value(usage, *keys):
    for key in keys:
        value = usage.get(key)
        if value is not None:
            return _as_number(value)
    return 0


def _first_usage_block(usage_payload):
    for key in ["usage", "provider_usage", "provider_raw_usage"]:
        block = usage_payload.get(key)
        if isinstance(block, dict) and block:
            return block
    return {}


def parse_cbc_stream_json_lines(lines):
    final_result = None
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"result", "turn.completed"}:
            final_result = event
    if not final_result:
        return {"usage_available": False, "source": "no_result_event"}
    provider_data = final_result.get("providerData") or {}
    usage = final_result.get("usage") or {}
    provider_usage = provider_data.get("usage") if isinstance(provider_data, dict) else {}
    provider_raw_usage = provider_data.get("rawUsage") if isinstance(provider_data, dict) else {}
    total_cost = final_result.get("total_cost_usd")
    available = any(
        [
            isinstance(usage, dict) and bool(usage),
            isinstance(provider_usage, dict) and bool(provider_usage),
            isinstance(provider_raw_usage, dict) and bool(provider_raw_usage),
            total_cost is not None,
        ]
    )
    return {
        "usage_available": bool(available),
        "source": "codex_json" if final_result.get("type") == "turn.completed" else "cbc_stream_json",
        "usage": usage if isinstance(usage, dict) else {},
        "provider_usage": provider_usage if isinstance(provider_usage, dict) else {},
        "provider_raw_usage": provider_raw_usage if isinstance(provider_raw_usage, dict) else {},
        "total_cost_usd": total_cost,
        "duration_ms": final_result.get("duration_ms"),
        "num_turns": final_result.get("num_turns"),
        "model": final_result.get("model"),
        "subtype": final_result.get("subtype"),
    }


def parse_cbc_stream_json(text):
    return parse_cbc_stream_json_lines(text.splitlines())


def load_usage_from_log(path):
    if not path:
        return {"usage_available": False, "source": "missing"}
    path_obj = Path(path)
    try:
        text = path_obj.read_text(encoding="utf-8")
    except OSError:
        return {"usage_available": False, "source": "missing", "path": str(path_obj)}
    result = parse_cbc_stream_json(text)
    result["path"] = str(path_obj)
    return result


def extract_attempt_usage(attempt_metadata):
    if not isinstance(attempt_metadata, dict):
        return {"usage_available": False, "source": "missing_metadata"}
    usage_payload = attempt_metadata.get("usage")
    if isinstance(usage_payload, dict):
        return usage_payload
    dispatch = attempt_metadata.get("dispatch") or {}
    return load_usage_from_log(dispatch.get("stdout_path"))


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
    )
    totals["total_cache_read_input_tokens"] += _usage_value(
        block,
        "cache_read_input_tokens",
        "cacheReadInputTokens",
    )
    cost = usage_payload.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        totals["total_cost_usd"] += float(cost)
    return True


def summarize_usage_payloads(usage_payloads):
    totals = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_input_tokens": 0,
        "total_cache_read_input_tokens": 0,
        "total_cost_usd": 0.0,
    }
    available = False
    for payload in usage_payloads:
        available = _add_usage_totals(totals, payload) or available
    return {
        "usage_available": available,
        "total_tokens": (totals["total_input_tokens"] + totals["total_output_tokens"]),
        **totals,
    }


def summarize_acceptance_metrics(conn):
    accepted_results, rejected_results = _result_counts(conn)
    published_actions, publish_failed_actions, deduplicated_actions = _action_counts(conn)
    payloads = []
    for row in db_read_worker_results.find_for_summarize_acceptance_metrics(conn):
        try:
            metadata = json.loads(row[0] or "{}")
        except ValueError:
            metadata = {}
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

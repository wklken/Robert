"""Usage and acceptance metrics helpers for Robert worker runs."""

from robert_agent.persistence import records as db_records
from robert_agent.persistence.queries import github_actions as db_read_github_actions
from robert_agent.persistence.queries import worker_results as db_read_worker_results

from robert_agent.foundation.values import json_object
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.adapters.processes import model_usage
import math
import sys
from itertools import chain
from datetime import timedelta


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
    inputs = _usage_value(block, "input_tokens", "inputTokens")
    if usage_payload.get("source") in {"cbc_stream_json", "claude_stream_json", "gateway", "opencode"}:
        inputs += _usage_value(block, "cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens")
        inputs += _usage_value(
            block, "cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write_input_tokens"
        )
    totals["total_input_tokens"] += inputs
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


def _historical_launch(payload, dispatch, started_at):
    segments = payload.get("segments")
    if (
        not isinstance(segments, dict)
        or not _launched(dispatch, started_at)
        or any(segment.get("launched") for segment in segments.values())
        or not any(
            _event_at(segment, event)
            for segment in segments.values()
            for event in ("artifact_adopted", "registered", "result_saved")
        )
    ):
        return payload
    # Registration of an old run can precede launch receipt support. Preserve
    # that original invocation without charging another call for adoption.
    return {
        **payload,
        "segments": {
            key: {**segment, "launched": True, "measurement_status": "unavailable"} for key, segment in segments.items()
        },
    }


def _attempt_usage_payloads(root, attempts, recorded):
    for attempt, model_runs in attempts:
        metadata = json_object(attempt["metadata_json"])
        dispatch = metadata.get("dispatch")
        dispatch = dispatch if isinstance(dispatch, dict) else {}
        saved = recorded.get(("worker", attempt["attempt_id"])) or metadata.get("usage")
        saved = saved if isinstance(saved, dict) else {}
        coordinator = dispatch.get("model_coordinator", bool(model_runs) and not saved.get("usage_available"))
        if not coordinator and (saved or _launched(dispatch, attempt["started_at"])):
            # Native coordinators are subprocess wrappers. Their model runs are
            # counted below, so an unmetered wrapper adds no extra model call.
            yield (
                _historical_launch(saved, dispatch, attempt["started_at"] if "started_at" in attempt.keys() else None)
                if saved
                else (model_usage.read_dispatch(root, dispatch) if attempt["finished_at"] else {})
            )
        for run in model_runs:
            dispatch = json_object(run["dispatch_json"])
            if recorded.get(("stage", run["run_id"])) or _launched(dispatch, run["started_at"]):
                saved = recorded.get(("stage", run["run_id"]))
                yield (
                    _historical_launch(saved, dispatch, run["started_at"])
                    if saved
                    else model_usage.read_dispatch(root, dispatch)
                )


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
    if source in {"cbc_stream_json", "claude_stream_json", "gateway", "opencode"} and inputs is not None:
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
        "pending_run_count": 0,
        "partial_run_count": 0,
        "complete_run_count": 0,
        "unavailable_run_count": 0,
    }
    auxiliary = (payload for (kind, _), payload in recorded.items() if kind not in {"worker", "stage"})
    payloads = list(chain(_attempt_usage_payloads(root, attempts, recorded), auxiliary))
    for payload in _launched_payloads(payloads):
        totals["run_count"] += 1
        status = payload.get("measurement_status")
        if status not in {"pending", "partial", "complete", "unavailable"}:
            status = (
                ("partial" if payload.get("partial") else "complete")
                if payload.get("usage_available")
                else "unavailable"
            )
        totals[f"{status}_run_count"] += 1
        tokens = _receipt_tokens(payload)
        for bucket, count in tokens.items():
            if count is not None:
                totals[bucket] = (totals[bucket] or 0) + count
        if tokens["input_tokens"] is not None and tokens["output_tokens"] is not None:
            totals["token_run_count"] += 1
            if (
                status == "complete"
                and not payload.get("partial")
                and all(count is not None for count in tokens.values())
            ):
                totals["billing_token_run_count"] += 1
        cost = payload.get("total_cost_usd")
        current_cost = totals["reported_cost_usd"] or 0.0
        if type(cost) in (int, float) and 0 <= cost <= sys.float_info.max - current_cost:
            if not payload.get("partial_cost"):
                totals["cost_run_count"] += 1
            totals["reported_cost_usd"] = current_cost + float(cost)
    if any(isinstance(payload.get("segments"), dict) for payload in payloads):
        return {**totals, **summarize_call_activity(payloads)}
    # Older consumers have an exact receipt shape; additive lifecycle fields
    # are projected when durable version 2 observations exist.
    return {
        key: value
        for key, value in totals.items()
        if key not in {"pending_run_count", "partial_run_count", "complete_run_count", "unavailable_run_count"}
    }


def _launched_payloads(payloads):
    for payload in payloads:
        segments = payload.get("segments")
        if isinstance(segments, dict):
            yield from (
                {
                    **{
                        key: value for key, value in payload.items() if key in {"source", "stage", "measurement_status"}
                    },
                    **segment,
                }
                for segment in segments.values()
                if isinstance(segment, dict) and segment.get("launched")
            )
        else:
            yield payload


def _event_at(segment, event):
    events = segment.get("events")
    fact = events.get(event) if isinstance(events, dict) else None
    return utc_timestamp(fact.get("at")) if isinstance(fact, dict) else None


def _union_seconds(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() for start, end in merged) if merged else None


def summarize_call_activity(payloads):
    """Project known activity separately from queues and post-model delays.

    An observed dead process is not an accurate model finish timestamp. Use
    reported duration or a specific model_completed fact; unknown stays null.
    Parallel segments contribute to activity sum and to an interval union.
    """
    payloads = list(payloads)
    result = _activity_totals(payloads)
    by_stage = {}
    queued = 0
    for payload in payloads:
        segments = payload.get("segments")
        for segment in segments.values() if isinstance(segments, dict) else (payload,):
            if not isinstance(segment, dict):
                continue
            if not segment.get("launched") and _event_at(segment, "queued"):
                queued += 1
            stage = segment.get("stage") or payload.get("stage")
            if isinstance(stage, str):
                by_stage.setdefault(stage, []).append({"segments": {"segment": segment}})
    return {
        **result,
        "queued_segment_count": queued,
        "by_stage": {stage: _activity_totals(segments) for stage, segments in by_stage.items()},
    }


def _activity_totals(payloads):
    activity, queue, post_model, intervals = [], [], [], []
    unknown = 0
    for segment in _launched_payloads(payloads):
        started, queued = _event_at(segment, "started"), _event_at(segment, "queued")
        if started and queued and started >= queued:
            queue.append((started - queued).total_seconds())
        duration = segment.get("duration_ms")
        seconds = duration / 1000 if type(duration) is int and 0 <= duration <= sys.maxsize else None
        completed = _event_at(segment, "model_completed")
        if seconds is None and completed and started and completed >= started:
            seconds = (completed - started).total_seconds()
        if seconds is None:
            unknown += 1
            continue
        activity.append(seconds)
        if completed is None and started:
            try:
                completed = started + timedelta(seconds=seconds)
            except OverflowError:
                continue
        if started and completed and completed >= started:
            intervals.append((started, completed))
        registered = _event_at(segment, "registered")
        if registered and completed and registered >= completed:
            post_model.append((registered - completed).total_seconds())
    return {
        "activity_seconds": sum(activity) if activity else None,
        "activity_union_seconds": _union_seconds(intervals),
        "measured_activity_count": len(activity),
        "unknown_activity_count": unknown,
        "queue_seconds": sum(queue) if queue else None,
        "post_model_seconds": sum(post_model) if post_model else None,
    }


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

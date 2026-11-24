"""Monotonic, caller-transaction-owned observations per model call and segment."""

import json

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import model_usage as queries


_RANK = {"pending": 0, "unavailable": 1, "partial": 2, "complete": 3}
_BLOCKS = ("usage", "provider_usage", "provider_raw_usage")


def _status(value):
    explicit = value.get("measurement_status")
    if explicit in _RANK:
        return explicit
    if value.get("usage_available"):
        return "partial" if value.get("partial") else "complete"
    return "unavailable"


def _segments(value, call_id):
    segments = value.get("segments")
    if isinstance(segments, dict):
        return {key: dict(segment) for key, segment in segments.items() if isinstance(segment, dict)}
    return {call_id: {**value, "launched": True, "measurement_status": _status(value)}}


def _merge_segment(previous, incoming):
    old_status, new_status = _status(previous), _status(incoming)
    upgrade = _RANK[new_status] > _RANK[old_status]
    grow_partial = new_status == old_status == "partial"
    result = dict(previous)
    if upgrade or grow_partial:
        result.update(incoming)
        for block in _BLOCKS:
            old, new = previous.get(block, {}), incoming.get(block, {})
            if isinstance(old, dict) and isinstance(new, dict) and (old or new):
                result[block] = dict(old)
                for key, count in new.items():
                    if type(count) is int and count >= 0:
                        result[block][key] = max(result[block].get(key, 0), count)
        costs = [
            value
            for value in (previous.get("total_cost_usd"), incoming.get("total_cost_usd"))
            if type(value) in (int, float) and value >= 0
        ]
        if costs:
            result["total_cost_usd"] = max(costs)
    else:
        # Same complete receipt and late lower-quality measurements are immutable.
        for key, value in incoming.items():
            if key not in result and key not in (
                *_BLOCKS,
                "total_cost_usd",
                "measurement_status",
                "partial",
                "partial_cost",
                "usage_available",
                "unknown_reason",
            ):
                result[key] = value
    result["launched"] = bool(previous.get("launched") or incoming.get("launched"))
    result["events"] = {**incoming.get("events", {}), **previous.get("events", {})}
    result["measurement_status"] = new_status if upgrade else old_status
    return result


def _aggregate(value, segments):
    result = {
        key: item for key, item in value.items() if key not in (*_BLOCKS, "total_cost_usd", "duration_ms", "segments")
    }
    launched = [segment for segment in segments.values() if segment.get("launched")]
    statuses = [_status(segment) for segment in launched]
    if not statuses:
        status = "unavailable" if any(_status(segment) == "unavailable" for segment in segments.values()) else "pending"
    elif "pending" in statuses:
        status = "pending"
    elif all(status == "complete" for status in statuses):
        status = "complete"
    elif any(segment.get("usage_available") for segment in launched):
        status = "partial"
    else:
        status = "unavailable"
    for block in _BLOCKS:
        counters = {}
        for segment in launched:
            for name, count in segment.get(block, {}).items():
                if type(count) is int and count >= 0:
                    counters[name] = counters.get(name, 0) + count
        if counters:
            result[block] = counters
    costs = [segment["total_cost_usd"] for segment in launched if type(segment.get("total_cost_usd")) in (int, float)]
    if costs:
        result["total_cost_usd"] = sum(costs)
    if len(launched) == 1 and "duration_ms" in launched[0]:
        result["duration_ms"] = launched[0]["duration_ms"]
    sources = {segment.get("source") for segment in launched if segment.get("source")}
    if len(sources) == 1:
        result["source"] = next(iter(sources))
    result.update(
        schema_version=2,
        segments=segments,
        measurement_status=status,
        usage_available=any(segment.get("usage_available") for segment in launched),
        partial=status == "partial",
        partial_cost=any(segment.get("partial_cost") or segment.get("total_cost_usd") is None for segment in launched),
    )
    return result


def record(conn, kind, call_id, scope_id, recorded_at, usage):
    """Acquire the SQLite writer before reading, merge, and never commit it here.

    Legacy callers still submit one launch observation. Version 2 observations
    carry separate segments so a resume adds only its normalized contribution.
    Scope identity cannot be rebound by an upsert.
    """
    usage = usage if isinstance(usage, dict) else {}
    incoming = _segments(usage, call_id)
    encoded = json.dumps(_aggregate(usage, incoming), allow_nan=False, sort_keys=True)
    inserted = statements.write(
        conn,
        insert(tables["model_usage"])
        .values(
            kind=kind,
            call_id=call_id,
            scope_id=scope_id,
            recorded_at=recorded_at,
            usage_json=encoded,
        )
        .on_conflict_do_nothing(),
    )
    if inserted:
        return inserted
    previous = queries.get(conn, kind, call_id)
    if previous is None or previous["scope_id"] != scope_id:
        return 0
    saved = json.loads(previous["usage_json"])
    segments = _segments(saved, call_id)
    for segment_id, segment in incoming.items():
        segments[segment_id] = _merge_segment(segments[segment_id], segment) if segment_id in segments else segment
    merged = json.dumps(_aggregate(saved, segments), allow_nan=False, sort_keys=True)
    if merged == previous["usage_json"]:
        return 0
    table = tables["model_usage"]
    return statements.write(
        conn,
        update(table)
        .where(table.c.kind == kind, table.c.call_id == call_id, table.c.scope_id == scope_id)
        .values(usage_json=merged),
    )

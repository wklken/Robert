"""Capture model launch, measurement and checkpoint facts independently."""

import json

from robert_agent.adapters.processes import model_usage
from robert_agent.foundation.values import utc_now
from robert_agent.persistence.queries import model_usage as queries
from robert_agent.persistence.stores import model_usage as store


def _segment(call_id, dispatch, segment_id):
    return segment_id or dispatch.get("usage_segment_id") or call_id


def _metadata(dispatch, stage):
    result = {
        key: value
        for key in (
            "worker_agent",
            "worker_name",
            "model",
            "reasoning_effort",
            "native_session_id",
            "parent_run_id",
            "logical_execution_id",
        )
        if isinstance(value := dispatch.get(key), str)
    }
    if stage is not None:
        result["stage"] = stage
    return result


def start_call(
    conn, root, kind, call_id, task_id, dispatch, *, stage=None, segment_id=None, started_at=None, confirmed=True
):
    """Record a launch request or an independently observed launch.

    Native coordinator wrappers have no model receipt. A new resume launch
    must use a new call_id or an explicit independent usage_segment_id.
    """
    if not isinstance(dispatch, dict) or dispatch.get("model_coordinator"):
        return 0
    at = started_at or utc_now()
    segment = {
        **_metadata(dispatch, stage),
        "source": model_usage.source_for_agent(dispatch.get("worker_agent")),
        "measurement_status": "pending",
        "usage_available": False,
        "launched": confirmed,
        "events": {
            ("started" if confirmed else "launch_requested"): {
                "at": at,
                "observed_at": at,
                "source": "controller",
                "precision": "observed",
            }
        },
    }
    return store.record(
        conn, kind, call_id, task_id, at, {"segments": {_segment(call_id, dispatch, segment_id): segment}}
    )


def record_dispatch_usage(
    conn,
    root,
    kind,
    call_id,
    task_id,
    dispatch,
    *,
    observed=None,
    stage=None,
    segment_id=None,
    finished_at=None,
    terminal=None,
):
    """Record terminal evidence even when a launch produced no usage.

    finished_at is when exit was observed, never inferred as model completion.
    Provider duration/source timestamps remain separate from that observation.
    """
    if root is None or not isinstance(dispatch, dict) or dispatch.get("model_coordinator"):
        return
    at = finished_at or utc_now()
    terminal = (
        bool(dispatch.get("process_exited") or dispatch.get("group_exit_confirmed")) if terminal is None else terminal
    )
    if observed is None:
        observed = model_usage.read_dispatch(root, dispatch)
        if (
            terminal
            and dispatch.get("native_usage_cursor")
            and (
                not observed.get("usage_available")
                or observed.get("partial")
                or "model_completed" not in observed.get("events", {})
            )
        ):
            prior = queries.get(conn, kind, call_id)
            segments = json.loads(prior["usage_json"]).get("segments", {}) if prior else {}
            previous = segments.get(_segment(call_id, dispatch, segment_id), {})
            if "process_exit_observed" not in previous.get("events", {}):
                native = model_usage.read_bound_resume_usage(dispatch)
                if native.get("usage_available") and (
                    not observed.get("usage_available") or (observed.get("partial") and not native.get("partial"))
                ):
                    observed = native
                elif native.get("events"):
                    observed = {**observed, "events": {**native["events"], **observed.get("events", {})}}
    observed = dict(observed) if isinstance(observed, dict) else {}
    launch_failed = (
        dispatch.get("ok") is False and dispatch.get("status") == "failed_dispatch" and not dispatch.get("pid")
    )
    if not observed.get("usage_available") and not terminal and not launch_failed:
        return 0
    status = observed.get("measurement_status")
    if status not in {"partial", "complete", "unavailable"}:
        status = (
            ("partial" if observed.get("partial") else "complete") if observed.get("usage_available") else "unavailable"
        )
    segment = {
        **_metadata(dispatch, stage),
        **observed,
        "source": observed.get("source") or model_usage.source_for_agent(dispatch.get("worker_agent")),
        "measurement_status": status,
        "usage_available": bool(observed.get("usage_available")),
        "launched": not launch_failed,
        "events": {
            **{key: {**value, "observed_at": at} for key, value in observed.get("events", {}).items()},
            "process_exit_observed" if terminal else "launch_failed" if launch_failed else "activity_observed": {
                "at": at,
                "observed_at": at,
                "source": "controller",
                "precision": "observed",
            },
        },
    }
    if not segment["usage_available"]:
        segment["unknown_reason"] = observed.get(
            "unknown_reason", "launch_failed" if launch_failed else "no_result_event"
        )
    return store.record(
        conn, kind, call_id, task_id, at, {"segments": {_segment(call_id, dispatch, segment_id): segment}}
    )


def record_call_event(conn, kind, call_id, task_id, event, *, at=None, stage=None, segment_id=None, metadata=None):
    """Append an idempotent queue/checkpoint event without launching a model.

    Reused/adopted artifacts use the original call_id; this never alters usage
    or creates an additional launched segment. Event metadata is allowlisted.
    """
    if event not in {
        "queued",
        "registered",
        "artifact_adopted",
        "result_saved",
        "delivered",
        "model_completed",
        "activity_observed",
    }:
        raise ValueError("unsupported model call event")
    at = at or utc_now()
    metadata = metadata if isinstance(metadata, dict) else {}
    facts = {
        key: value
        for key in ("binding_digest", "artifact_sha256", "source", "precision")
        if isinstance(value := metadata.get(key), str)
    }
    segment = {
        "measurement_status": "pending",
        "usage_available": False,
        "launched": False,
        "events": {event: {"at": at, "observed_at": at, **facts}},
    }
    if stage is not None:
        segment["stage"] = stage
    return store.record(conn, kind, call_id, task_id, at, {"segments": {segment_id or call_id: segment}})

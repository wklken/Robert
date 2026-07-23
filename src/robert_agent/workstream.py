#!/usr/bin/env python3
import argparse
import json
import sys

from robert_agent.common import emit


def resolve_workstream_id(event):
    return event.get("workstream_id") or event["source_key"]


def source_relationship(event):
    metadata = event.get("metadata") or {}
    if (
        event.get("source_type") == "pull_request"
        and (event.get("origin_workstream_id") or metadata.get("dd_workstream"))
    ):
        return "derived_pr"
    return "primary"


def plan_event(event, active_workstreams=None):
    active_workstreams = set(active_workstreams or [])
    workstream_id = resolve_workstream_id(event)
    fingerprint = event["event_fingerprint"]
    if workstream_id in active_workstreams:
        action = "append_pending_event"
        pending_events = [fingerprint]
    else:
        action = "create_task"
        pending_events = []

    return {
        "ok": True,
        "action": action,
        "workstream_id": workstream_id,
        "source_key": event["source_key"],
        "source_relationship": source_relationship(event),
        "event_fingerprint": fingerprint,
        "pending_events": pending_events,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--active-workstream", action="append", default=[])
    args = parser.parse_args(argv)
    event = json.load(sys.stdin)
    result = plan_event(event, active_workstreams=set(args.active_workstream))
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())

"""Calendar windows and completed work based on audited execution records."""

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robert_agent.foundation.contracts.delivery import (
    AuditStatus,
    OutputType,
    PublishStatus,
    VerificationStatus,
)
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.foundation.values import json_list, json_object
from robert_agent.persistence import database, records, transactions
from robert_agent.persistence.queries import agent_sessions, sqlite_master
from robert_agent.persistence.queries.readviews import task_history
from robert_agent.persistence.queries.readviews import timeline as timeline_queries
from robert_agent.readmodels import usage
from robert_agent.security.redaction import redact_text
from robert_agent.security.text import safe_public_link, safe_text


class TimelineQueryError(ValueError):
    pass


def _period(view, anchor):
    if view == "day":
        return anchor, anchor + timedelta(days=1)
    if view == "week":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=7)
    if view == "month":
        start = anchor.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1)
    raise TimelineQueryError("view must be day, week, or month")


def _published_actions(output_type, actions):
    if any(
        action["audit_status"] != AuditStatus.ACCEPTED
        or action["publish_status"] not in {PublishStatus.PUBLISHED, PublishStatus.SKIPPED}
        for action in actions
    ):
        return None
    published = [action for action in actions if action["publish_status"] == PublishStatus.PUBLISHED]
    if not published and output_type != OutputType.LOCAL_RESULT:
        return None
    return published


def _completion_time(result, published):
    receipt_times = [utc_timestamp(action["published_at"]) for action in published]
    published_at = max(receipt_times) if receipt_times and all(receipt_times) else None
    return published_at or utc_timestamp(result["local_result_at"]) or utc_timestamp(result["task_updated_at"])


def _entry(result, published, completed_at):
    links = {}
    for action in published:
        url = safe_public_link(action["target_url"])
        if url:
            links.setdefault(url, {"type": action["action_type"], "url": url})
    return {
        "task_id": result["task_id"],
        "result_id": result["result_id"],
        "work_item_id": result["work_item_id"],
        "repository": safe_text(result["repository"], 300),
        "project": safe_text((result["request_project"] or "").rsplit("/", 1)[-1], 100),
        "title": safe_text(result["work_item_title"] or result["request_title"], 400),
        "source_url": safe_public_link(result["request_url"]),
        "output_type": result["output_type"],
        "completed_at": completed_at.isoformat(),
        "deliveries": list(links.values()),
    }


def _result_actions(conn, results):
    actions = defaultdict(list)
    for action in task_history.timeline_actions_for_results(conn, [row["result_id"] for row in results]):
        actions[action["result_id"]].append(action)
    return actions


def _zone(timezone_name):
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise TimelineQueryError("invalid time zone") from exc


def list_completed(db_path, *, view="week", anchor_date=None, timezone_name="UTC"):
    zone = _zone(timezone_name)
    if anchor_date is None:
        anchor = datetime.now(zone).date()
    else:
        try:
            anchor = date.fromisoformat(anchor_date)
        except ValueError as exc:
            raise TimelineQueryError("date must be YYYY-MM-DD") from exc
        if anchor.isoformat() != anchor_date or not 2 <= anchor.year <= 9998:
            raise TimelineQueryError("date must be YYYY-MM-DD with a year from 0002 to 9998")
    start, end = _period(view, anchor)
    previous = start - timedelta(days=1)
    # The indexed text bounds include dates written with offsets. The parsed
    # local date below applies the exact calendar bounds, including DST.
    earliest = datetime.combine(start, time.min, zone).astimezone(timezone.utc).date() - timedelta(days=1)
    latest = datetime.combine(end, time.min, zone).astimezone(timezone.utc).date() + timedelta(days=2)
    with closing(database.connect(db_path, busy_timeout_ms=5000)) as conn:
        transactions.begin(conn)
        results = timeline_queries.completed_results(conn, earliest.isoformat(), latest.isoformat())
        actions = _result_actions(conn, results)

    completed = []
    for row in results:
        published = _published_actions(row["output_type"], actions[row["result_id"]])
        if published is None:
            continue
        occurred = _completion_time(row, published)
        if occurred is not None:
            local = occurred.astimezone(zone)
            if start <= local.date() < end:
                completed.append(_entry(row, published, local))
    return {
        "view": view,
        "timezone": timezone_name,
        "start": start.isoformat(),
        "end": (end - timedelta(days=1)).isoformat(),
        "previous": previous.isoformat(),
        "next": end.isoformat(),
        "entries": sorted(
            completed, key=lambda entry: (utc_timestamp(entry["completed_at"]), entry["task_id"]), reverse=True
        ),
    }


def live_display(db_path, *, timezone_name="UTC", now=None):
    zone = _zone(timezone_name)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    week_start, week_end = _period("week", today)
    with closing(database.connect(db_path, busy_timeout_ms=5000)) as conn:
        transactions.begin(conn)
        results = timeline_queries.completed_results(conn, include_context=False)
        actions = _result_actions(conn, results)
        running = timeline_queries.running_task_count(conn)

    counts = {"total": 0, "week": 0, "today": 0, "running": running}
    receipts = []
    for row in results:
        published = _published_actions(row["output_type"], actions[row["result_id"]])
        if published is None or (occurred := _completion_time(row, published)) is None:
            continue
        local_date = occurred.astimezone(zone).date()
        counts["total"] += 1
        counts["week"] += week_start <= local_date < week_end
        counts["today"] += local_date == today
        receipts.append((occurred, row["task_id"], row["result_id"]))
    # Comparing identities across snapshots also catches newly confirmed work
    # with older completion times and multiple results sharing a timestamp.
    return {"counts": counts, "receipts": [[task_id, result_id] for _, task_id, result_id in sorted(receipts)]}


def _receipt_attempts(conn, task_id, row):
    cutoff = utc_timestamp(row["result_created_at"])
    for attempt in timeline_queries.receipt_attempts(conn, task_id, row["attempt_no"], row["result_created_at"]):
        model_runs = agent_sessions.model_runs_for_attempt(conn, attempt["task_id"], attempt["attempt_id"], limit=None)
        yield (
            attempt,
            [run for run in model_runs if cutoff is None or (utc_timestamp(run["created_at"]) or cutoff) <= cutoff],
        )


def get_receipt(db_path, task_id, result_id, *, include_usage=True, include_entry=False):
    with closing(database.connect(db_path, busy_timeout_ms=5000)) as conn:
        transactions.begin(conn)
        row = records.first(timeline_queries.completed_result_receipt(conn, task_id, result_id))
        if row is None:
            return None
        actions = task_history.timeline_actions_for_results(conn, [result_id])
        published = _published_actions(row["output_type"], actions)
        if published is None:
            return None
        entry = None
        if include_entry:
            result = records.first(timeline_queries.completed_results(conn, receipt=(task_id, result_id)))
            occurred = _completion_time(result, published) if result is not None else None
            if occurred is None:
                return None
            entry = _entry(result, published, occurred)
        attempts = list(_receipt_attempts(conn, task_id, row)) if include_usage else []
        observations = (
            timeline_queries.receipt_usage(
                conn, {task_id, *(attempt["task_id"] for attempt, _ in attempts)}, row["result_created_at"]
            )
            if include_usage and sqlite_master.object_exists(conn, "model_usage")
            else []
        )
        recorded = {(item["kind"], item["call_id"]): json_object(item["usage_json"]) for item in observations}

    started = utc_timestamp(row["started_at"])
    finished = utc_timestamp(row["finished_at"])
    summary = row["receipt_summary"]
    safe_summary = redact_text(summary, strict_assignments=True) if isinstance(summary, str) else {}
    checks = [check for check in json_list(row["verification_json"]) if isinstance(check, dict)]
    return {
        **({"entry": entry} if entry is not None else {}),
        "summary": safe_summary.get("text", ""),
        "summary_redacted": safe_summary.get("ok") is False,
        "summary_truncated": False,
        "worker": safe_text(row["worker"], 100),
        "model": safe_text(row["model"], 100),
        "attempt": row["attempt_no"],
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "duration_seconds": (finished - started).total_seconds()
        if started and finished and finished >= started
        else None,
        **(
            {
                "usage": usage.summarize_receipt_usage(
                    Path(db_path).expanduser().absolute().parent, attempts, recorded=recorded
                )
            }
            if include_usage
            else {}
        ),
        "verification": {
            status.value: sum(check.get("status") == status.value for check in checks) for status in VerificationStatus
        },
    }

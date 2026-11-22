"""Calendar windows and completed work based on audited execution records."""

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from robert_agent.foundation.contracts.delivery import AuditStatus, OutputType, PublishStatus, VerificationStatus
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.foundation.values import json_list
from robert_agent.persistence import database, records, transactions
from robert_agent.persistence.queries.readviews import task_history
from robert_agent.persistence.queries.readviews import timeline as timeline_queries
from robert_agent.security.redaction import redact_and_truncate
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


def _deliveries(result, actions):
    published = _published_actions(result["output_type"], actions)
    if published is None:
        return None
    links = {}
    for action in published:
        url = safe_public_link(action["target_url"])
        if url:
            links.setdefault(url, {"type": action["action_type"], "url": url})
    receipt_times = [utc_timestamp(action["published_at"]) for action in published]
    completed_at = max(receipt_times) if receipt_times and all(receipt_times) else None
    occurred = completed_at or utc_timestamp(result["local_result_at"]) or utc_timestamp(result["task_updated_at"])
    return occurred, list(links.values())


def list_completed(db_path, *, view="week", anchor_date=None, timezone_name="UTC"):
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise TimelineQueryError("invalid time zone") from exc
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
        actions = defaultdict(list)
        for action in task_history.timeline_actions_for_results(conn, [row["result_id"] for row in results]):
            actions[action["result_id"]].append(action)

    completed = []
    for row in results:
        delivery = _deliveries(row, actions[row["result_id"]])
        if delivery is None:
            continue
        occurred, links = delivery
        if occurred is None:
            continue
        local = occurred.astimezone(zone)
        if not start <= local.date() < end:
            continue
        completed.append(
            {
                "task_id": row["task_id"],
                "result_id": row["result_id"],
                "work_item_id": row["work_item_id"],
                "repository": safe_text(row["repository"], 300),
                "project": safe_text((row["request_project"] or "").rsplit("/", 1)[-1], 100),
                "title": safe_text(row["work_item_title"] or row["request_title"], 400),
                "source_url": safe_public_link(row["request_url"]),
                "output_type": row["output_type"],
                "completed_at": local.isoformat(),
                "deliveries": links,
            }
        )
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


def get_receipt(db_path, task_id, result_id):
    with closing(database.connect(db_path, busy_timeout_ms=5000)) as conn:
        transactions.begin(conn)
        row = records.first(timeline_queries.completed_result_receipt(conn, task_id, result_id))
        if row is None:
            return None
        actions = task_history.timeline_actions_for_results(conn, [result_id])
        if _published_actions(row["output_type"], actions) is None:
            return None

    started = utc_timestamp(row["started_at"])
    finished = utc_timestamp(row["finished_at"])
    summary = (
        redact_and_truncate(row["handoff"], 1600, strict_assignments=True) if isinstance(row["handoff"], str) else {}
    )
    checks = [check for check in json_list(row["verification_json"]) if isinstance(check, dict)]
    return {
        "summary": summary.get("text", "").strip(),
        "summary_redacted": summary.get("ok") is False,
        "summary_truncated": summary.get("truncated", False),
        "worker": safe_text(row["worker"], 100),
        "model": safe_text(row["model"], 100),
        "attempt": row["attempt_no"],
        "started_at": started.isoformat() if started else None,
        "duration_seconds": (finished - started).total_seconds()
        if started and finished and finished >= started
        else None,
        "verification": {
            status.value: sum(check.get("status") == status.value for check in checks) for status in VerificationStatus
        },
    }

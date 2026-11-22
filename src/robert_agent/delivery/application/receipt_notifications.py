"""Send separate receipts together through the existing OpenClaw destination."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from robert_agent.adapters.notifications import messaging as transport, receipt_image
from robert_agent.delivery import messages
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.foundation.values import json_object, new_id
from robert_agent.persistence import database, records, transactions
from robert_agent.persistence.queries import dispatch_requests, notifications as queries
from robert_agent.persistence.stores import notifications as stores
from robert_agent.readmodels.timeline import get_receipt
from robert_agent.work.errors import WorkItemValidationError
from robert_agent.work.messaging_identity import authenticate
from robert_agent.work.receipt_events import receipt_settings


BATCH_INTERVAL = timedelta(minutes=30)
# Telegram photo albums accept up to ten items. Overflow stays queued.
BATCH_LIMIT = 10


def _can_deliver(conn, config, task_id):
    request = dispatch_requests.for_task(conn, task_id) if task_id else None
    if request is None:
        return True
    try:
        authenticate(config, json_object(request["identity_json"]))
    except WorkItemValidationError:
        return False
    return True


def _collect(conn, config, signal, occurred, scope):
    events, after, destination = signal
    allowed = messages.allowed_repo_ids(conn, config)
    if scope is not None:
        allowed = allowed.intersection(scope)
    if not allowed:
        return []
    latest = records.first(queries.latest_receipt_digest(conn, destination))
    if latest and utc_timestamp(latest[0]) > occurred - BATCH_INTERVAL:
        return []
    pending = queries.receipt_events(
        conn,
        destination=destination,
        after=after.isoformat(),
        now=occurred.isoformat(),
        kinds=events,
        repo_ids=allowed,
        messaging_settings=config["messaging"],
        limit=BATCH_LIMIT,
    )
    if not pending or utc_timestamp(pending[0]["created_at"]) > occurred - BATCH_INTERVAL:
        return []
    return pending


def has_due(conn, config, now, *, repo_ids=None):
    signal = receipt_settings(config)
    occurred = utc_timestamp(now)
    return bool(
        signal
        and occurred
        and not os.environ.get("ROBERT_NOTIFICATION_COMMAND")
        and _collect(conn, config, signal, occurred, repo_ids)
    )


def _receipt_rows(db_path, pending):
    rows = []
    for notice in pending:
        metadata = json_object(notice["metadata_json"])
        details = (
            get_receipt(db_path, notice["task_id"], metadata["result_id"], include_usage=False)
            if notice["task_id"] and metadata.get("result_id")
            else None
        )
        summary = (details or {}).get("summary", "")
        url = metadata.get("url") or ""
        rows.append(
            {
                "event": notice["notification_type"],
                "title": metadata.get("title"),
                "repository": metadata["repository"],
                "occurred_at": utc_timestamp(notice["created_at"]).strftime("%Y-%m-%d %H:%M UTC"),
                "summary": summary,
                "url": url if len(url) <= 256 else "",
            }
        )
    return rows


def _set_state(db_path, batch_id, status, **details):
    with closing(database.connect(db_path)) as conn, conn:
        stores.set_delivery_state(conn, batch_id, json.dumps({"status": status, **details}))


def deliver_pending(config, *, repo_ids=None, now=None, render=receipt_image.render_pngs, send=transport.send):
    signal = receipt_settings(config)
    if signal is None:
        return {"status": "disabled"}
    if os.environ.get("ROBERT_NOTIFICATION_COMMAND"):
        return {"status": "unsupported_transport"}
    db_path = config["db_path"]
    occurred = utc_timestamp(now) if now else datetime.now(timezone.utc)
    if occurred is None:
        return {"status": "invalid_clock"}
    with task_publication_lock(db_path, "agent-receipt-digest") as locked:
        if not locked:
            return {"status": "busy"}
        with closing(database.connect(db_path, busy_timeout_ms=5000)) as conn, conn:
            transactions.begin_immediate(conn)
            pending = _collect(conn, config, signal, occurred, repo_ids)
            if not pending:
                return {"status": "waiting"}
            batch_id = new_id("receipt-digest")
            stores.record_notification(
                conn,
                batch_id,
                None,
                "agent_receipt_digest",
                "receipt",
                "recorded",
                occurred.isoformat(),
                json.dumps({"destination": signal[2], "delivery": {"status": "rendering"}}),
            )

        media_root = Path(config["data_dir"]) / "receipt-media"
        try:
            media_root.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            _set_state(db_path, batch_id, "render_failed")
            return {"status": "render_failed"}
        with TemporaryDirectory(prefix="batch-", dir=media_root) as directory:
            try:
                media = []
                with TemporaryDirectory(prefix="robert-receipt-render-") as rendering:
                    pngs = render(
                        _receipt_rows(db_path, pending), rendering, config["messaging"]["receipts"]["chromium"]
                    )
                    if len(pngs) != len(pending):
                        raise ValueError("Expected one PNG for each pending receipt")
                    for index, png in enumerate(pngs, start=1):
                        path = Path(directory) / f"robert-receipt-{index:02}.png"
                        shutil.copyfile(png, path)
                        media.append(path)
            except (OSError, subprocess.SubprocessError, ValueError):
                _set_state(db_path, batch_id, "render_failed")
                return {"status": "render_failed"}

            with closing(database.connect(db_path, busy_timeout_ms=5000)) as conn, conn:
                transactions.begin_immediate(conn)
                allowed = messages.allowed_repo_ids(conn, config)
                if receipt_settings(config) != signal or any(
                    json_object(row["metadata_json"])["repo_id"] not in allowed
                    or not _can_deliver(conn, config, row["task_id"])
                    for row in pending
                ):
                    stores.set_delivery_state(conn, batch_id, json.dumps({"status": "scope_changed"}))
                    return {"status": "scope_changed"}
                for row in pending:
                    stores.set_delivery_state(
                        conn, row["notification_id"], json.dumps({"status": "claimed", "batch_id": batch_id})
                    )
                stores.set_delivery_state(
                    conn,
                    batch_id,
                    json.dumps({"status": "sending", "events": [row["notification_id"] for row in pending]}),
                )

            # A timeout can occur after OpenClaw has already sent the attachment.
            # Keep the durable claims and avoid a second push for the same milestones.
            receipt = {}
            sent = send(
                {
                    "type": "agent_receipt_digest",
                    "delivery_id": batch_id,
                    "summary": f"{len(pending)} 项关键交付，详见小票。",
                    "actions": [],
                },
                config=config,
                media_paths=media,
                receipt=receipt,
            )
            status = "sent" if sent else "delivery_unknown"
            _set_state(db_path, batch_id, status, **receipt, events=[row["notification_id"] for row in pending])
            return {"status": status, "count": len(pending)}

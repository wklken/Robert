"""Final Worker results survive a busy domain database and process restart."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def directory(db_path):
    return Path(db_path).resolve().parent / (Path(db_path).name + ".results")


@contextmanager
def locked_record(db_path, attempt_id, *, blocking=True):
    root = directory(db_path)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(str(attempt_id).encode()).hexdigest()
    path = root / (key + ".json")
    # Stable lock inode is never removed while another writer may hold it.
    with (root / (key + ".lock")).open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield path


def atomic_write(path, record):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        temporary = Path(f.name)
        try:
            json.dump(record, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def submit(db_path, payload, writer, notifier=None, *, blocking=True):
    from robert_agent import wakeup_notifier

    attempt_id = payload.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id or not payload.get("task_id"):
        return {
            "ok": False,
            "status": "failed_validation",
            "safe_error": "task_id and attempt_id are required",
        }
    digest = fingerprint(payload)
    with locked_record(db_path, attempt_id, blocking=blocking) as path:
        saved_at = datetime.now(timezone.utc).isoformat()
        if path.exists():
            existing = json.loads(path.read_text())
            if existing.get("fingerprint") == digest:
                saved_at = existing.get("saved_at") or saved_at
            if (
                existing.get("fingerprint") != digest
                and existing.get("status") == "pending"
            ):
                return {
                    "ok": False,
                    "status": "result_conflict",
                    "safe_error": "A different final result is already pending for this attempt.",
                }
        atomic_write(
            path, {"status": "pending", "fingerprint": digest, "payload": payload, "saved_at": saved_at}
        )
        try:
            response = writer(db_path, payload, notifier=notifier)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            wakeup_notifier.notify(db_path, sender=notifier)
            return {
                "ok": True,
                "status": "deferred",
                "safe_error": "Result saved locally; waiting for database ingestion.",
            }
        if response["ok"] or response["status"] == "failed_validation":
            path.unlink(missing_ok=True)
        else:
            atomic_write(
                path,
                {
                    "status": "rejected",
                    "fingerprint": digest,
                    "payload": payload,
                    "reason": response["status"],
                },
            )
        return response


def replay(db_path, writer, limit=100):
    counts = {"recorded": 0, "deferred": 0, "rejected": 0}
    processed = 0
    deadline = time.monotonic() + 2
    for path in sorted(directory(db_path).glob("*.json")):
        if processed >= limit or time.monotonic() >= deadline:
            break
        try:
            record = json.loads(path.read_text())
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("payload"), dict)
                or not record["payload"].get("attempt_id")
            ):
                raise ValueError("invalid result spool shape")
        except (ValueError, OSError):
            # Preserve corrupt evidence but do not let it block healthy results.
            if path.exists():
                path.rename(path.with_suffix(".invalid"))
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    "INSERT OR IGNORE INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json) VALUES (?,'worker_result_invalid','local','recorded',strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),?)",
                    (
                        "notification-invalid-" + path.stem,
                        json.dumps(
                            {
                                "reason_code": "result_spool_invalid",
                                "required_action": "Inspect the quarantined result spool file.",
                            }
                        ),
                    ),
                )
            counts["rejected"] += 1
            continue
        if record.get("status") != "pending":
            continue
        processed += 1
        payload = record["payload"]
        try:
            response = submit(db_path, payload, writer, blocking=False)
        except BlockingIOError:
            counts["deferred"] += 1
            continue
        except Exception as exc:
            record["ingestion_failures"] = int(record.get("ingestion_failures", 0)) + 1
            record["reason"] = type(exc).__name__
            if record["ingestion_failures"] >= 3:
                record["status"] = "rejected"
            with locked_record(db_path, payload["attempt_id"]) as locked_path:
                # A concurrent successful ingestion may already have removed it.
                if not locked_path.exists():
                    continue
                current = json.loads(locked_path.read_text())
                if current.get("status") != "pending" or current.get(
                    "fingerprint"
                ) != record.get("fingerprint"):
                    continue
                atomic_write(locked_path, record)
            if record["status"] == "pending":
                counts["deferred"] += 1
                continue
            response = {"ok": False, "status": "result_ingestion_exhausted"}
        if response["status"] == "deferred":
            counts["deferred"] += 1
            break  # One busy database blocks every result; retry next cycle.
        elif response["ok"]:
            counts["recorded"] += 1
        else:
            counts["rejected"] += 1
            with closing(sqlite3.connect(db_path)) as conn, conn:
                notification_id = "notification-result-rejected-" + fingerprint(
                    {"attempt_id": payload["attempt_id"]}
                )
                conn.execute(
                    "INSERT OR IGNORE INTO notifications(notification_id,task_id,notification_type,channel,status,created_at,metadata_json) VALUES (?,?,'worker_result_rejected','local','recorded',strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),?)",
                    (
                        notification_id,
                        payload["task_id"],
                        json.dumps(
                            {
                                "attempt_id": payload["attempt_id"],
                                "reason_code": response["status"],
                            }
                        ),
                    ),
                )
    return counts


def saved_at(db_path, attempt_id):
    """Read the timestamp while submit owns this attempt's spool lock."""
    key = hashlib.sha256(str(attempt_id).encode()).hexdigest()
    try:
        return json.loads((directory(db_path) / (key + ".json")).read_text()).get("saved_at")
    except (OSError, ValueError):
        return None

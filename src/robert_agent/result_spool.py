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

from robert_agent import result_contract


def directory(db_path):
    return Path(db_path).resolve().parent / (Path(db_path).name + ".results")


@contextmanager
def locked_record(db_path, attempt_id, *, blocking=True):
    root = directory(db_path)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(str(attempt_id).encode()).hexdigest()
    path = root / (key + ".json")
    with _locked_path(path, blocking=blocking):
        yield path


@contextmanager
def _locked_path(path, *, blocking=False):
    # Validation/quarantine use the same stable lock inode as submission.
    with path.with_suffix(".lock").open("a") as lock:
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


def _archive_rejected(path, record):
    archive = path.parent / 'rejected'
    archive.mkdir(mode=0o700, exist_ok=True)
    if archive.is_symlink():
        raise ValueError('result archive must not be a symlink')
    atomic_write(archive / path.name, record)
    path.unlink(missing_ok=True)


def _validate_record(record):
    if (not isinstance(record, dict) or record.get("status") != "pending"
            or type(record.get("normalizer_version")) is not int or record["normalizer_version"] < 1
            or record.get("result_contract") != result_contract.CONTRACT_ID
            or not isinstance(record.get("payload"), dict)
            or not isinstance(record.get("fingerprint"), str)
            or record["payload"].get("_submission_fingerprint") != record["fingerprint"]
            or record["payload"].get("_result_contract") != result_contract.CONTRACT_ID
            or not record["payload"].get("attempt_id")):
        raise ValueError("unsupported or invalid result spool contract")


def _ingest(db_path, path, record, writer, notifier):
    from robert_agent import wakeup_notifier
    try:
        response = writer(db_path, record["payload"], notifier=notifier)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        wakeup_notifier.notify(db_path, sender=notifier)
        return {"ok": True, "status": "deferred", "safe_error": "Result durably saved; waiting for ingestion."}
    if response["ok"]:
        path.unlink(missing_ok=True)
    else:
        _archive_rejected(path, {**record, "status": "rejected", "reason": response["status"]})
    return response


def submit(db_path, envelope, writer, notifier=None, *, prepare, lookup):
    """Serialize semantic deduplication, preparation and durable final-slot writes."""
    digest = result_contract.semantic_fingerprint(envelope)
    with locked_record(db_path, envelope["attempt_id"]) as path:
        if path.exists():
            try:
                record = json.loads(path.read_text())
                _validate_record(record)
            except (ValueError, OSError, TypeError):
                return {"ok": False, "status": "unsupported_contract", "safe_error": "Existing spool requires manual reconciliation before submission."}
            if record["fingerprint"] != digest:
                return {"ok": False, "status": "result_conflict", "safe_error": "A different final result is pending for this attempt."}
            return _ingest(db_path, path, record, writer, notifier)
        existing = lookup(db_path, envelope, digest)
        if existing:
            return existing
        payload, rejection = prepare(db_path, envelope)
        if rejection:
            return rejection
        payload.update(_submission_fingerprint=digest, _result_contract=result_contract.CONTRACT_ID,
                       _normalizer_version=result_contract.NORMALIZER_VERSION)
        record = {"status": "pending", "result_contract": result_contract.CONTRACT_ID,
                  "normalizer_version": result_contract.NORMALIZER_VERSION, "fingerprint": digest,
                  "payload": payload, "saved_at": datetime.now(timezone.utc).isoformat()}
        atomic_write(path, record)
        return _ingest(db_path, path, record, writer, notifier)


def _notify_rejection(db_path, path, reason):
    try:
        with closing(sqlite3.connect(db_path, timeout=0.1)) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json) VALUES (?,'worker_result_invalid','local','recorded',strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),?)",
                         ("notification-invalid-" + path.stem, json.dumps({"reason_code": reason, "required_action": "Inspect preserved spool evidence before retrying."})))
    except sqlite3.Error:
        pass  # The preserved file remains authoritative diagnostic evidence.


def replay(db_path, writer, limit=100):
    counts = {"recorded": 0, "deferred": 0, "rejected": 0}
    deadline = time.monotonic() + 2
    for path in sorted(directory(db_path).glob("*.json"))[:limit]:
        if time.monotonic() >= deadline:
            break
        try:
            with _locked_path(path):
                if not path.exists():
                    continue
                try:
                    record = json.loads(path.read_text())
                    _validate_record(record)
                    expected = hashlib.sha256(record["payload"]["attempt_id"].encode()).hexdigest() + ".json"
                    if path.name != expected:
                        raise ValueError("spool identity differs from its path")
                except (ValueError, OSError, TypeError):
                    if path.exists():
                        path.rename(path.with_suffix(".invalid"))
                    _notify_rejection(db_path, path, "unsupported_spool_contract")
                    counts["rejected"] += 1
                    continue
                try:
                    response = _ingest(db_path, path, record, writer, None)
                except Exception as exc:
                    record["ingestion_failures"] = int(record.get("ingestion_failures", 0)) + 1
                    record["reason"] = type(exc).__name__
                    if record["ingestion_failures"] < 3:
                        atomic_write(path, record)
                        counts["deferred"] += 1
                        continue
                    _archive_rejected(path, {**record, "status": "rejected"})
                    response = {"ok": False, "status": "result_ingestion_exhausted"}
        except BlockingIOError:
            counts["deferred"] += 1
            continue
        if response["status"] == "deferred":
            counts["deferred"] += 1
            break
        if response["ok"]:
            counts["recorded"] += 1
        else:
            counts["rejected"] += 1
            _notify_rejection(db_path, path, response["status"])
    return counts


def saved_at(db_path, attempt_id):
    """Read the timestamp while submit owns this attempt's spool lock."""
    key = hashlib.sha256(str(attempt_id).encode()).hexdigest()
    try:
        return json.loads((directory(db_path) / (key + ".json")).read_text()).get("saved_at")
    except (OSError, ValueError):
        return None

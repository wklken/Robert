#!/usr/bin/env python3
import argparse
from contextlib import closing
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from robert_agent.common import emit
from robert_agent.worker import snapshot

DEFAULT_TAIL_BYTES = 8192
DEFAULT_TAIL_LINES = 80
MIN_HEARTBEAT_INTERVAL_SECONDS = 0.1


def build_command_record(task_id, attempt_id, phase, command):
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "phase": phase,
        "active_command": list(command),
    }


def _safe_filename_part(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return value.strip("._") or "command"


def _default_output_dir(db_path, task_id):
    return Path(db_path).expanduser().parent / "artifacts" / task_id


def _tail_file(path, max_bytes=DEFAULT_TAIL_BYTES, max_lines=DEFAULT_TAIL_LINES):
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return {
            "path": str(path),
            "bytes": 0,
            "tail": "",
            "truncated": False,
        }
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    line_truncated = len(lines) > max_lines
    if line_truncated:
        text = "\n".join(lines[-max_lines:])
    return {
        "path": str(path),
        "bytes": size,
        "tail": text,
        "truncated": size > max_bytes or line_truncated,
    }


def process_identity(pid):
    """Identify a process incarnation, not just a recyclable PID."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        return f"{boot}:{stat.rsplit(')', 1)[1].split()[19]}"
    except (OSError, IndexError):
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""


def _write_command_record(path, record):
    # Rename a flushed file so readers never see a partial terminal record.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        try:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _record_command_snapshot(**kwargs):
    try:
        snapshot.record_snapshot(**kwargs)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        return "deferred"
    return "recorded"


def _kill_command(proc):
    # Keep the worker's process group so supervisor cancellation still owns all
    # children; for an individual command timeout, stop only its descendants.
    try:
        if os.name == "posix":
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5,
            )
            children = {}
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) == 2 and all(field.isdigit() for field in fields):
                    children.setdefault(int(fields[1]), []).append(int(fields[0]))
            descendants = [proc.pid]
            for pid in descendants:
                descendants.extend(children.get(pid, []))
            for pid in reversed(descendants[1:]):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def run_command_with_heartbeat(
    db_path,
    task_id,
    attempt_id,
    phase,
    command,
    timeout_seconds=5400,
    heartbeat_interval_seconds=60,
    output_dir=None,
    tail_bytes=DEFAULT_TAIL_BYTES,
    tail_lines=DEFAULT_TAIL_LINES,
):
    # Validate ownership with a read before allowing a busy write to be deferred.
    snapshot.build_snapshot(task_id, attempt_id, phase, "running", "Starting command")
    with closing(sqlite3.connect(db_path)) as conn:
        if not conn.execute(
            "SELECT 1 FROM attempts WHERE attempt_id = ? AND task_id = ?",
            (attempt_id, task_id),
        ).fetchone():
            raise ValueError("attempt_id does not belong to task_id")
    output_dir = Path(output_dir).expanduser() if output_dir else _default_output_dir(db_path, task_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_prefix = f"{_safe_filename_part(attempt_id)}.{_safe_filename_part(phase)}.{time.time_ns()}"
    stdout_path = output_dir / f"{output_prefix}.stdout.log"
    stderr_path = output_dir / f"{output_prefix}.stderr.log"
    record_dir = _default_output_dir(db_path, task_id)
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / f"{output_prefix}.command.json"
    record = {
        "task_id": task_id, "attempt_id": attempt_id, "phase": phase,
        "status": "starting", "wrapper_pid": os.getpid(),
        "wrapper_identity": process_identity(os.getpid()),
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_command_record(record_path, record)
    _record_command_snapshot(
        db_path=db_path,
        task_id=task_id,
        attempt_id=attempt_id,
        phase=phase,
        status="running",
        summary="Starting command",
        next_step="Wait for command completion",
    )
    started = time.monotonic()
    timed_out = False
    heartbeat_interval_seconds = max(
        float(heartbeat_interval_seconds),
        MIN_HEARTBEAT_INTERVAL_SECONDS,
    )
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        try:
            proc = subprocess.Popen(command, stdout=stdout_file, stderr=stderr_file)
        except OSError as exc:
            record.update(
                status="failed", returncode=None,
                summary=f"Command failed to start: {type(exc).__name__}",
                finished_at=datetime.now(timezone.utc).isoformat(), snapshot_status="deferred",
            )
            _write_command_record(record_path, record)
            raise
        try:
            record.update(status="running", pid=proc.pid, process_identity=process_identity(proc.pid))
            _write_command_record(record_path, record)
            while True:
                if proc.poll() is not None:
                    break
                elapsed = time.monotonic() - started
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    timed_out = True
                    _kill_command(proc)
                    break
                try:
                    proc.wait(timeout=min(heartbeat_interval_seconds, remaining))
                except subprocess.TimeoutExpired:
                    pass
                else:
                    break
                if proc.poll() is None:
                    _record_command_snapshot(
                        db_path=db_path,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        phase=phase,
                        status="running",
                        summary="Command still running",
                        next_step="Wait for command completion",
                    )
        except BaseException as exc:
            _kill_command(proc)
            record.update(
                status="failed", returncode=proc.returncode,
                summary=f"Command wrapper failed: {type(exc).__name__}",
                finished_at=datetime.now(timezone.utc).isoformat(), snapshot_status="deferred",
            )
            _write_command_record(record_path, record)
            raise
    stdout_info = _tail_file(stdout_path, max_bytes=tail_bytes, max_lines=tail_lines)
    stderr_info = _tail_file(stderr_path, max_bytes=tail_bytes, max_lines=tail_lines)
    returncode = None if timed_out else proc.returncode
    if timed_out:
        status = "failed"
        summary = f"Command timed out after {timeout_seconds} seconds"
    else:
        status = "completed" if returncode == 0 else "failed"
        summary = f"Command exited {returncode}"
    record.update(
        status=status, returncode=returncode, timed_out=timed_out, summary=summary,
        finished_at=datetime.now(timezone.utc).isoformat(), snapshot_status="deferred",
    )
    _write_command_record(record_path, record)
    snapshot_status = _record_command_snapshot(
        db_path=db_path,
        task_id=task_id,
        attempt_id=attempt_id,
        phase=phase,
        status=status,
        summary=summary,
        next_step="Inspect saved command output and record result",
    )
    record["snapshot_status"] = snapshot_status
    _write_command_record(record_path, record)
    return {
        "ok": returncode == 0,
        "status": status,
        "returncode": returncode,
        "command_record_path": str(record_path),
        "snapshot_status": snapshot_status,
        "stdout_path": stdout_info["path"],
        "stderr_path": stderr_info["path"],
        "stdout_bytes": stdout_info["bytes"],
        "stderr_bytes": stderr_info["bytes"],
        "stdout_tail": stdout_info["tail"],
        "stderr_tail": stderr_info["tail"],
        "stdout_truncated": stdout_info["truncated"],
        "stderr_truncated": stderr_info["truncated"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=60)
    parser.add_argument("--output-dir")
    parser.add_argument("--tail-bytes", type=int, default=DEFAULT_TAIL_BYTES)
    parser.add_argument("--tail-lines", type=int, default=DEFAULT_TAIL_LINES)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.db:
        result = run_command_with_heartbeat(
            db_path=args.db,
            task_id=args.task_id,
            attempt_id=args.attempt_id,
            phase=args.phase,
            command=command,
            timeout_seconds=args.timeout_seconds,
            heartbeat_interval_seconds=args.heartbeat_interval_seconds,
            output_dir=args.output_dir,
            tail_bytes=args.tail_bytes,
            tail_lines=args.tail_lines,
        )
    else:
        result = {
            "ok": True,
            "status": "built",
            "command": build_command_record(args.task_id, args.attempt_id, args.phase, command),
        }
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())

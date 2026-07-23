#!/usr/bin/env python3
import argparse
import re
import subprocess
import time
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
    snapshot.record_snapshot(
        db_path=db_path,
        task_id=task_id,
        attempt_id=attempt_id,
        phase=phase,
        status="running",
        summary="Starting command",
        next_step="Wait for command completion",
    )
    output_dir = Path(output_dir).expanduser() if output_dir else _default_output_dir(db_path, task_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_prefix = f"{_safe_filename_part(attempt_id)}.{_safe_filename_part(phase)}.{time.time_ns()}"
    stdout_path = output_dir / f"{output_prefix}.stdout.log"
    stderr_path = output_dir / f"{output_prefix}.stderr.log"
    started = time.monotonic()
    timed_out = False
    heartbeat_interval_seconds = max(
        float(heartbeat_interval_seconds),
        MIN_HEARTBEAT_INTERVAL_SECONDS,
    )
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(
            command,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        while True:
            if proc.poll() is not None:
                break
            elapsed = time.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                timed_out = True
                proc.kill()
                proc.wait()
                break
            time.sleep(min(heartbeat_interval_seconds, remaining))
            if proc.poll() is None:
                snapshot.record_snapshot(
                    db_path=db_path,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    phase=phase,
                    status="running",
                    summary="Command still running",
                    next_step="Wait for command completion",
                )
    stdout_info = _tail_file(stdout_path, max_bytes=tail_bytes, max_lines=tail_lines)
    stderr_info = _tail_file(stderr_path, max_bytes=tail_bytes, max_lines=tail_lines)
    returncode = None if timed_out else proc.returncode
    if timed_out:
        status = "failed"
        summary = f"Command timed out after {timeout_seconds} seconds"
    else:
        status = "completed" if returncode == 0 else "failed"
        summary = f"Command exited {returncode}"
    snapshot.record_snapshot(
        db_path=db_path,
        task_id=task_id,
        attempt_id=attempt_id,
        phase=phase,
        status=status,
        summary=summary,
        next_step="Inspect saved command output and record result",
    )
    return {
        "ok": returncode == 0,
        "status": status,
        "returncode": returncode,
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

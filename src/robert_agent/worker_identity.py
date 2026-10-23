"""Process identity capture and verified Worker process-group operations.

The Worker PID is only a locator.  A recorded Worker identity also includes
the process group, platform start token, observed command fingerprint, and the
reader used to collect those values.  A later observation is therefore either
the exact same Worker, definitely dead, or unknown; unknown is deliberately
not treated as a safe retry/termination signal.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time


IDENTITY_SOURCE_PROC = "proc"
IDENTITY_SOURCE_PS = "ps"
_IDENTITY_FIELDS = (
    "pid",
    "process_group_id",
    "process_start_token",
    "command_fingerprint",
    "identity_source",
)
_ZOMBIE_STATES = {"Z", "X", "x"}


class IdentityReadError(RuntimeError):
    """The process exists or visibility is ambiguous, but identity is unreadable."""


class ProcessIdentityMissing(IdentityReadError):
    """The process no longer exists."""


class EmptyCommandline(IdentityReadError):
    """The process command line was visible but empty."""


class ZombieProcess(IdentityReadError):
    """The observed process is a zombie and cannot be a running Worker."""


def _fingerprint(command_bytes):
    if not isinstance(command_bytes, (bytes, bytearray, memoryview)):
        command_bytes = str(command_bytes).encode("utf-8")
    return hashlib.sha256(bytes(command_bytes)).hexdigest()


def parse_proc_stat(stat_text, *, pid=None):
    """Parse the stable fields from Linux ``/proc/<pid>/stat``.

    The command name is parenthesized and may itself contain spaces or closing
    parentheses.  Splitting after the final closing parenthesis avoids the
    usual ``split()`` parsing bug.
    """

    if isinstance(stat_text, (bytes, bytearray, memoryview)):
        stat_text = bytes(stat_text).decode("utf-8", errors="strict")
    if not isinstance(stat_text, str):
        raise IdentityReadError("process stat is not text")
    close = stat_text.rfind(")")
    if close <= 0:
        raise IdentityReadError("malformed process stat")
    prefix = stat_text[:close]
    try:
        observed_pid = int(prefix.split("(", 1)[0].strip())
    except (IndexError, TypeError, ValueError) as exc:
        raise IdentityReadError("malformed process stat pid") from exc
    if pid is not None and observed_pid != int(pid):
        raise IdentityReadError("process stat pid mismatch")
    fields = stat_text[close + 1 :].strip().split()
    # Fields after comm begin at field 3.  pgrp is field 5 (offset 2), and
    # starttime is field 22 (offset 19).
    if len(fields) <= 19:
        raise IdentityReadError("process stat is truncated")
    state = fields[0]
    try:
        process_group_id = int(fields[2])
    except (TypeError, ValueError) as exc:
        raise IdentityReadError("malformed process group") from exc
    process_start_token = fields[19]
    if not state or not process_start_token or process_group_id <= 0:
        raise IdentityReadError("incomplete process stat identity")
    return {
        "pid": observed_pid,
        "process_state": state,
        "process_group_id": process_group_id,
        "process_start_token": str(process_start_token),
    }


def read_proc_identity(
    pid,
    *,
    proc_root="/proc",
    read_text=None,
    read_bytes=None,
):
    """Read a complete identity from Linux ``/proc``.

    ``read_text`` and ``read_bytes`` are injectable for deterministic tests.
    File/permission errors intentionally propagate so callers can distinguish
    a missing process from a process whose identity is not visible.
    """

    try:
        pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise IdentityReadError("invalid pid") from exc
    if pid <= 0:
        raise IdentityReadError("invalid pid")
    stat_path = Path(proc_root) / str(pid) / "stat"
    cmdline_path = Path(proc_root) / str(pid) / "cmdline"
    read_text = read_text or (lambda path: Path(path).read_text(encoding="utf-8"))
    read_bytes = read_bytes or (lambda path: Path(path).read_bytes())
    identity = parse_proc_stat(read_text(stat_path), pid=pid)
    command_bytes = read_bytes(cmdline_path)
    if not isinstance(command_bytes, (bytes, bytearray, memoryview)):
        raise IdentityReadError("process cmdline is not bytes")
    if not command_bytes:
        raise EmptyCommandline("process cmdline is empty")
    identity.update(
        {
            "command_fingerprint": _fingerprint(command_bytes),
            "identity_source": IDENTITY_SOURCE_PROC,
        }
    )
    return identity


def _parse_ps_line(line, pid):
    parts = (line or "").strip().split()
    if len(parts) < 5:
        raise IdentityReadError("portable process identity is truncated")
    try:
        observed_pid = int(parts[0])
        process_group_id = int(parts[2])
    except (TypeError, ValueError) as exc:
        raise IdentityReadError("malformed portable process identity") from exc
    if observed_pid != int(pid) or process_group_id <= 0:
        raise IdentityReadError("portable process identity mismatch")
    state = parts[1]
    # macOS/BSD lstart is five whitespace-separated tokens.  Test doubles and
    # other ps implementations may expose one token; accept both shapes while
    # keeping the observed command separate from the start token.
    if len(parts) >= 9:
        start_token = " ".join(parts[3:8])
        command = " ".join(parts[8:])
    else:
        start_token = parts[3]
        command = " ".join(parts[4:])
    if not state or not start_token or not command:
        raise IdentityReadError("portable process identity is incomplete")
    return {
        "pid": observed_pid,
        "process_state": state[0],
        "process_group_id": process_group_id,
        "process_start_token": start_token,
        "command_fingerprint": _fingerprint(command.encode("utf-8")),
        "identity_source": IDENTITY_SOURCE_PS,
    }


def read_ps_identity(pid, *, ps_runner=None):
    """Read a complete identity with one bounded portable ``ps`` call."""

    try:
        pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise IdentityReadError("invalid pid") from exc
    if pid <= 0:
        raise IdentityReadError("invalid pid")
    runner = ps_runner or subprocess.run
    command = ["ps", "-p", str(pid), "-o", "pid=,stat=,pgid=,lstart=,command="]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityReadError("portable process identity command failed") from exc
    if getattr(completed, "returncode", 1) != 0:
        # A non-zero command with no diagnostic is the normal "no such PID"
        # result.  Diagnostics indicate a visibility/permission failure.
        if getattr(completed, "stderr", ""):
            raise IdentityReadError("portable process identity is not visible")
        raise ProcessIdentityMissing("portable process is not present")
    lines = [line for line in (getattr(completed, "stdout", "") or "").splitlines() if line.strip()]
    if not lines:
        raise ProcessIdentityMissing("portable process is not present")
    return _parse_ps_line(lines[0], pid)


def capture_process_identity(pid, *, proc_root="/proc", ps_runner=None):
    """Capture one live process identity, preferring proc and falling back to ps."""

    try:
        identity = read_proc_identity(pid, proc_root=proc_root)
    except ProcessIdentityMissing:
        raise
    except (EmptyCommandline, ZombieProcess):
        # A visible but invalid running-process observation must fail closed;
        # falling back to a different reader would weaken the dispatch gate.
        raise
    except (FileNotFoundError, ProcessLookupError):
        # A missing proc mount/entry uses the one-call portable fallback.  The
        # fallback itself distinguishes an empty result (dead) from a failed
        # or permission-denied command (unknown).
        identity = read_ps_identity(pid, ps_runner=ps_runner)
    except (PermissionError, OSError, IdentityReadError):
        # proc can be unavailable on BSD/macOS or denied by policy; use the
        # single-call fallback.  It is still source-tagged as ps for replay.
        identity = read_ps_identity(pid, ps_runner=ps_runner)
    if identity.get("process_state") in _ZOMBIE_STATES:
        raise ZombieProcess("process is a zombie")
    return identity


def _valid_identity(identity):
    if not isinstance(identity, dict):
        return False
    for field in _IDENTITY_FIELDS:
        if identity.get(field) in (None, ""):
            return False
    try:
        if int(identity["pid"]) <= 0 or int(identity["process_group_id"]) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if identity.get("identity_source") not in {
        IDENTITY_SOURCE_PROC,
        IDENTITY_SOURCE_PS,
    }:
        return False
    fingerprint = str(identity.get("command_fingerprint"))
    return len(fingerprint) == 64 and all(char in "0123456789abcdef" for char in fingerprint)


def compare_recorded_identity(recorded, observed):
    """Compare two identity records without treating PID alone as proof."""

    if not _valid_identity(recorded) or not _valid_identity(observed):
        return "unknown"
    if observed.get("process_state") in _ZOMBIE_STATES:
        return "dead"
    for field in (
        "pid",
        "process_group_id",
        "process_start_token",
        "command_fingerprint",
        "identity_source",
    ):
        if str(recorded.get(field)) != str(observed.get(field)):
            return "unknown"
    return "alive_same"


def validate_identity(identity):
    """Return whether all persisted identity fields are structurally valid."""

    return _valid_identity(identity)


def _observe_recorded_identity(recorded, *, proc_root="/proc", ps_runner=None):
    if not _valid_identity(recorded):
        return "unknown"
    pid = int(recorded["pid"])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "unknown"
    except OSError:
        return "unknown"
    try:
        if recorded["identity_source"] == IDENTITY_SOURCE_PROC:
            observed = read_proc_identity(pid, proc_root=proc_root)
        else:
            observed = read_ps_identity(pid, ps_runner=ps_runner)
    except (ProcessIdentityMissing, FileNotFoundError, ProcessLookupError):
        # A proc entry can disappear after the initial existence check.  If
        # the PID is genuinely gone it is dead; if it remains visible, the
        # missing entry is an observation ambiguity (PID reuse/visibility),
        # never proof that the recorded Worker is dead.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except (PermissionError, OSError):
            return "unknown"
        return "unknown"
    except (OSError, IdentityReadError):
        return "unknown"
    return compare_recorded_identity(recorded, observed)


def inspect_recorded_identity(recorded, *, proc_root="/proc", ps_runner=None):
    """Return ``alive_same``, ``dead``, or ``unknown`` for recorded metadata."""

    return _observe_recorded_identity(recorded, proc_root=proc_root, ps_runner=ps_runner)


def _default_clock():
    return time.monotonic()


def terminate_recorded_identity(
    recorded,
    *,
    inspect=None,
    signal_group=None,
    supervisor_process_group_id=None,
    clock=None,
    sleep=None,
    timeout_seconds=2.0,
    poll_interval=0.05,
):
    """Safely terminate a verified isolated process group.

    The identity is checked before TERM and on every observation after TERM.
    KILL is only sent when the exact same identity remains alive.  Any
    visibility loss/mismatch returns ``attention`` without another signal.
    """

    inspect = inspect or inspect_recorded_identity
    signal_group = signal_group or os.killpg
    supervisor_process_group_id = (
        os.getpgrp() if supervisor_process_group_id is None else supervisor_process_group_id
    )
    clock = clock or _default_clock
    sleep = sleep or time.sleep
    if not _valid_identity(recorded):
        return {"status": "attention", "reason": "invalid_identity"}
    pid = int(recorded["pid"])
    process_group_id = int(recorded["process_group_id"])
    if process_group_id != pid or process_group_id == int(supervisor_process_group_id):
        return {
            "status": "attention",
            "reason": "unsafe_process_group",
            "pid": pid,
            "process_group_id": process_group_id,
        }
    initial = inspect(recorded)
    if initial == "dead":
        return {"status": "already_dead", "pid": pid}
    if initial != "alive_same":
        return {"status": "attention", "reason": "identity_not_verified", "pid": pid}
    try:
        signal_group(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "already_dead", "pid": pid, "signal": "SIGTERM"}
    except (PermissionError, OSError) as exc:
        return {
            "status": "attention",
            "reason": "term_failed",
            "pid": pid,
            "error_type": type(exc).__name__,
        }
    deadline = clock() + max(0.0, float(timeout_seconds))
    while True:
        observed = inspect(recorded)
        if observed == "dead":
            return {"status": "terminated", "pid": pid, "signal": "SIGTERM"}
        if observed != "alive_same":
            return {"status": "attention", "reason": "identity_changed", "pid": pid}
        current = clock()
        if current >= deadline:
            break
        sleep(max(0.0, min(float(poll_interval), deadline - current)))
    try:
        signal_group(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return {"status": "terminated", "pid": pid, "signal": "SIGKILL"}
    except (PermissionError, OSError) as exc:
        return {
            "status": "attention",
            "reason": "kill_failed",
            "pid": pid,
            "error_type": type(exc).__name__,
        }
    kill_deadline = clock() + max(0.0, float(timeout_seconds))
    while True:
        observed = inspect(recorded)
        if observed == "dead":
            return {"status": "terminated", "pid": pid, "signal": "SIGKILL"}
        if observed != "alive_same":
            return {"status": "attention", "reason": "identity_changed", "pid": pid}
        current = clock()
        if current >= kill_deadline:
            return {"status": "attention", "reason": "termination_timeout", "pid": pid}
        sleep(max(0.0, min(float(poll_interval), kill_deadline - current)))


def cleanup_new_process(proc, *, signal_group=None, wait_timeout=2.0):
    """Best-effort cleanup for a newly-created child whose identity was unreadable."""

    if proc is None:
        return
    signal_group = signal_group or os.killpg
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            signal_group(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=wait_timeout)
        return
    except (AttributeError, subprocess.TimeoutExpired, OSError):
        pass
    if pid:
        try:
            signal_group(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=wait_timeout)
    except (AttributeError, subprocess.TimeoutExpired, OSError):
        # The in-hand Popen is still given a final direct kill/reap attempt; it
        # cannot be confused with a reused PID because it is the just-created
        # child object.
        try:
            proc.kill()
        except (AttributeError, OSError):
            pass
        try:
            proc.wait(timeout=wait_timeout)
        except (AttributeError, subprocess.TimeoutExpired, OSError):
            pass

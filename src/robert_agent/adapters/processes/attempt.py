"""Process evidence for attempts; no task state or scheduling authority."""
from pathlib import Path
import subprocess

from robert_agent.adapters.processes import identity as worker_identity


def _proc_process_state(pid):
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        remainder = stat_text.rsplit(")", 1)[1].strip()
    except IndexError:
        return None
    fields = remainder.split()
    return fields[0] if fields else None


def _ps_process_state(pid):
    try:
        completed = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    stat = (completed.stdout or "").strip().split()
    return stat[0][0] if stat and stat[0] else None


def process_state(pid):
    return _proc_process_state(pid) or _ps_process_state(pid)


def dispatch_status(metadata):
    dispatch_metadata = (metadata or {}).get("dispatch") or {}
    pid = dispatch_metadata.get("pid")
    if dispatch_metadata.get('process_exited') and type(pid) is int and pid > 1:
        # This is a persisted Popen outcome from the dispatcher, not a Worker
        # claim. A fast child can exit before an identity snapshot is captured.
        try:
            status = 'unknown' if worker_identity.live_process_group(pid) else 'dead'
            return {'status': status, 'pid': pid, 'reason': 'recorded_process_exit'}
        except (OSError, subprocess.TimeoutExpired):
            return {'status': 'unknown', 'pid': pid, 'reason': 'group_inspection_failed'}
    try:
        result = worker_identity.inspect_recorded_identity(dispatch_metadata)
    except Exception:
        result = "unknown"
    if result not in {"alive_same", "dead", "unknown"}:
        result = "unknown"
    if result == "dead":
        try:
            if worker_identity.live_process_group(pid):
                return {"status": "unknown", "pid": pid, "reason": "unverified_descendants_running"}
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "unknown", "pid": pid, "reason": "group_inspection_failed"}
    payload = {
        "status": result,
        "pid": pid,
        "identity_source": dispatch_metadata.get("identity_source"),
    }
    if result == "dead":
        payload["reason"] = "process_missing_or_zombie"
    elif result == "unknown":
        payload["reason"] = "identity_unverified"
    return payload


def terminate_attempt(metadata):
    dispatch_metadata = (metadata or {}).get("dispatch") or {}
    if dispatch_metadata.get('cleanup_pending'):
        pid = dispatch_metadata.get('pid')
        if type(pid) is int and pid > 1:
            try:
                if not worker_identity.live_process_group(pid):
                    return {'status': 'already_dead', 'pid': pid}
            except (OSError, subprocess.TimeoutExpired):
                pass
        # Without a captured identity, later recovery must never signal this PID.
        return {'status': 'attention', 'reason': 'startup_cleanup_unconfirmed'}
    outcome = worker_identity.terminate_recorded_identity(dispatch_metadata)
    if outcome.get("status") in {"terminated", "already_dead"}:
        try:
            if worker_identity.live_process_group(dispatch_metadata.get("pid")):
                return {"status": "attention", "reason": "unverified_descendants_running"}
        except (OSError, subprocess.TimeoutExpired):
            return {"status": "attention", "reason": "group_inspection_failed"}
    return outcome


def group_stopped(pgid):
    """Failed observations never prove that ownership can be released."""
    try:
        return not worker_identity.live_process_group(pgid)
    except (OSError, subprocess.TimeoutExpired):
        return False

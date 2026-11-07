"""Safe error payloads for controller runs."""
import json
import subprocess


def safe_discovery_error(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]
        command = [str(part) for part in cmd]
        return {
            "status": "failed_discovery",
            "safe_error": (
                f"GitHub discovery command failed with exit {exc.returncode}: "
                + " ".join(command)
            ),
            "returncode": exc.returncode,
            "command": command,
        }
    if isinstance(exc, json.JSONDecodeError):
        return {
            "status": "failed_discovery",
            "safe_error": "GitHub discovery returned invalid JSON",
            "message": exc.msg,
        }
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return {
            "status": "failed_discovery",
            "safe_error": f"GitHub discovery returned invalid event data: {exc}",
            "error_type": type(exc).__name__,
        }
    return {
        "status": "failed_discovery",
        "safe_error": "GitHub discovery failed",
    }


def safe_dispatch_error(exc):
    return {
        "ok": False,
        "status": "failed_dispatch",
        "safe_error": f"worker dispatch failed: {exc}",
        "error_type": type(exc).__name__,
    }


def safe_publish_error(exc):
    return {
        "ok": False,
        "status": "publish_failed",
        "safe_error": f"GitHub action publication failed: {exc}",
        "error_type": type(exc).__name__,
        "pending_count": 0,
        "published_count": 0,
        "deduplicated_count": 0,
        "skipped_count": 0,
        "failed_count": 1,
    }


def safe_route_error(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return {
            'ok': False, 'status': 'failed_route', 'error_type': type(exc).__name__,
            'safe_error': f'Worktree preparation command timed out after {exc.timeout} seconds.',
        }
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]
        command = [str(part) for part in cmd]
        return {
            "ok": False,
            "status": "failed_route",
            "safe_error": (
                f"worktree preparation command failed with exit {exc.returncode}: "
                + " ".join(command)
            ),
            "returncode": exc.returncode,
            "command": command,
        }
    return {
        "ok": False,
        "status": "failed_route",
        "safe_error": "worktree preparation failed",
        "error_type": type(exc).__name__,
    }

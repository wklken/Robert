#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from robert_agent import acceptance
from robert_agent.common import emit
from robert_agent import controlled_e2e_acceptance
from robert_agent import live_discovery_acceptance
from robert_agent import live_worker_acceptance
from robert_agent import live_worktree_acceptance
from robert_agent import production_write_canary
from robert_agent import publish_dedupe_acceptance


COMMENT_URL_RE = re.compile(r"^/([^/]+)/([^/]+)/(?:issues|pull)/[0-9]+$")


def _workspace_dir(workspace_dir):
    if workspace_dir:
        path = Path(workspace_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="dd-acceptance-suite-")).resolve()


def _default_checks():
    return {
        "preflight": lambda **kwargs: acceptance.acceptance_preflight(kwargs["config_path"]),
        "live_discovery": lambda **kwargs: live_discovery_acceptance.live_discovery_acceptance(
            kwargs["config_path"],
            limit=kwargs.get("discovery_limit", 30),
        ),
        "controlled_e2e": lambda **kwargs: controlled_e2e_acceptance.controlled_e2e_acceptance(
            kwargs["config_path"],
            workspace_dir=kwargs["workspace_dir"] / "controlled-e2e",
            timeout_seconds=kwargs.get("timeout_seconds", 30),
            poll_interval_seconds=kwargs.get("poll_interval_seconds", 0.2),
        ),
        "live_worktree": lambda **kwargs: live_worktree_acceptance.live_worktree_acceptance(
            kwargs["config_path"],
            workspace_dir=kwargs["workspace_dir"] / "live-worktree",
        ),
        "publish_dedupe": lambda **kwargs: publish_dedupe_acceptance.publish_dedupe_acceptance(
            workspace_dir=kwargs["workspace_dir"] / "publish-dedupe",
        ),
        "live_worker": lambda **kwargs: live_worker_acceptance.live_worker_acceptance(
            kwargs["config_path"],
            workspace_dir=kwargs["workspace_dir"] / "live-worker",
            timeout_seconds=kwargs.get("timeout_seconds", 600),
            poll_interval_seconds=kwargs.get("poll_interval_seconds", 2),
        ),
    }


def _run_check(name, check, kwargs):
    started = time.monotonic()
    try:
        result = check(**kwargs)
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed_exception",
            "safe_error": f"{type(exc).__name__}: {exc}",
        }
    elapsed = time.monotonic() - started
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status", "unknown"),
        "elapsed_seconds": round(elapsed, 3),
        "result": result,
    }


def _command_failure(completed, fallback):
    if getattr(completed, "returncode", 0) == 0:
        return ""
    return (getattr(completed, "stderr", "") or fallback).strip()


def _verify_production_canary_evidence(evidence_url, marker_id, run_command=subprocess.run):
    parsed = urlparse(evidence_url or "")
    match = COMMENT_URL_RE.match(parsed.path)
    fragment = parsed.fragment or ""
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not match:
        return {
            "ok": False,
            "status": "invalid_evidence_url",
            "safe_error": "production canary evidence must be a GitHub issue or PR comment URL",
        }
    comment_match = re.fullmatch(r"issuecomment-([0-9]+)", fragment)
    if not comment_match:
        return {
            "ok": False,
            "status": "invalid_evidence_url",
            "safe_error": "production canary evidence URL must include an issuecomment fragment",
        }
    owner, repo = match.groups()
    comment_id = comment_match.group(1)
    command = ["gh", "api", f"repos/{owner}/{repo}/issues/comments/{comment_id}"]
    completed = run_command(command, capture_output=True, text=True)
    safe_error = _command_failure(completed, "gh api comment evidence lookup failed")
    if safe_error:
        return {
            "ok": False,
            "status": "evidence_lookup_failed",
            "safe_error": safe_error,
            "command": command,
        }
    try:
        comment = json.loads(getattr(completed, "stdout", "") or "{}")
    except ValueError:
        comment = {}
    body = comment.get("body") if isinstance(comment.get("body"), str) else ""
    marker = f"production-canary:{marker_id}" if marker_id else "production-canary:"
    if marker not in body:
        return {
            "ok": False,
            "status": "marker_mismatch",
            "safe_error": "production canary comment does not contain the expected marker",
            "command": command,
        }
    return {
        "ok": True,
        "status": "verified",
        "comment_id": str(comment.get("id") or comment_id),
        "comment_url": comment.get("html_url") or evidence_url,
        "marker_id": marker_id,
        "command": command,
    }


def acceptance_suite(
    config_path,
    workspace_dir=None,
    include_live_worker=False,
    checks=None,
    timeout_seconds=600,
    poll_interval_seconds=2,
    discovery_limit=30,
    production_canary_target_url=None,
    production_canary_marker_id=None,
    production_canary_evidence_url=None,
    production_canary_run_command=subprocess.run,
):
    workspace = _workspace_dir(workspace_dir)
    checks = checks or _default_checks()
    kwargs = {
        "config_path": Path(config_path),
        "workspace_dir": workspace,
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "discovery_limit": discovery_limit,
    }
    ordered = [
        "preflight",
        "live_discovery",
        "controlled_e2e",
        "live_worktree",
        "publish_dedupe",
    ]
    results = {}
    for name in ordered:
        results[name] = _run_check(name, checks[name], kwargs)
    if include_live_worker:
        results["live_worker"] = _run_check("live_worker", checks["live_worker"], kwargs)
    else:
        results["live_worker"] = {
            "ok": None,
            "status": "skipped",
            "elapsed_seconds": 0,
            "result": {
                "ok": None,
                "status": "skipped",
                "safe_error": "live worker check skipped; rerun with --include-live-worker",
            },
        }

    required_safe_checks = ordered + (["live_worker"] if include_live_worker else [])
    safe_acceptance_ok = all(results[name]["ok"] for name in required_safe_checks)
    failed_checks = [
        name
        for name in required_safe_checks
        if not results[name]["ok"]
    ]
    production_write = {
        "ok": False,
        "status": "required",
        "safe_error": (
            "Real GitHub publication is intentionally not performed by this safe suite. "
            "Run an explicitly approved production canary before claiming production write readiness."
        ),
        "canary_command": (
            "python3 src/robert_agent/production_write_canary.py "
            "--target-url <github-issue-or-pr-url> --confirm-github-write"
        ),
    }
    if production_canary_target_url:
        canary_plan = production_write_canary.production_write_canary(
            production_canary_target_url,
            workspace_dir=workspace / "production-write-canary",
            marker_id=production_canary_marker_id,
            confirm_github_write=False,
        )
        production_write["canary_plan"] = canary_plan
        if canary_plan.get("ok"):
            production_write["canary_command"] = (
                "python3 src/robert_agent/production_write_canary.py "
                f"--target-url {production_canary_target_url} "
                f"--marker-id {canary_plan['marker_id']} "
                "--confirm-github-write"
            )
    if production_canary_evidence_url:
        canary_evidence = _verify_production_canary_evidence(
            production_canary_evidence_url,
            production_canary_marker_id,
            run_command=production_canary_run_command,
        )
        production_write["evidence"] = canary_evidence
        if canary_evidence.get("ok"):
            production_write.update(
                {
                    "ok": True,
                    "status": "verified",
                    "safe_error": "",
                }
            )
    production_write_ok = bool(production_write.get("ok"))
    if failed_checks:
        status = "blocked"
        ok = False
        next_actions = [f"Fix failed safe checks: {', '.join(failed_checks)}"]
    elif production_write_ok:
        status = "completed"
        ok = True
        next_actions = []
    else:
        status = "incomplete"
        ok = False
        next_actions = ["Run an explicitly approved production GitHub write canary"]
    return {
        "ok": ok,
        "status": status,
        "safe_acceptance_ok": safe_acceptance_ok,
        "workspace_dir": str(workspace),
        "checks": results,
        "failed_checks": failed_checks,
        "production_write": production_write,
        "next_actions": next_actions,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--workspace-dir")
    parser.add_argument("--include-live-worker", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--poll-interval-seconds", type=float, default=2)
    parser.add_argument("--discovery-limit", type=int, default=30)
    parser.add_argument("--production-canary-target-url")
    parser.add_argument("--production-canary-marker-id")
    parser.add_argument("--production-canary-evidence-url")
    args = parser.parse_args(argv)
    result = acceptance_suite(
        args.config,
        workspace_dir=args.workspace_dir,
        include_live_worker=args.include_live_worker,
        timeout_seconds=args.timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        discovery_limit=args.discovery_limit,
        production_canary_target_url=args.production_canary_target_url,
        production_canary_marker_id=args.production_canary_marker_id,
        production_canary_evidence_url=args.production_canary_evidence_url,
    )
    if result["status"] == "blocked":
        return emit(result, 2)
    return emit(result, 0)


if __name__ == "__main__":
    raise SystemExit(main())

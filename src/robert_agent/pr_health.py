"""Read-only GitHub PR health and CI evidence normalization."""

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone

from robert_agent import discover, redaction


SUCCESSFUL_CONCLUSIONS = {"success", "neutral", "skipped"}
MANUAL_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "stale",
    "startup_failure",
    "timed_out",
}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b"
)
WHITESPACE_RE = re.compile(r"[ \t]+")


def _mapping(value):
    return value if isinstance(value, dict) else {}


def fetch_pr_snapshot(repo_full_name, number, runner=subprocess.run):
    payload = discover._try_run_json(
        ["gh", "api", f"repos/{repo_full_name}/pulls/{number}"],
        runner=runner,
    )
    if not isinstance(payload, dict):
        return None
    head = _mapping(payload.get("head"))
    base = _mapping(payload.get("base"))
    head_repo = _mapping(head.get("repo"))
    author = _mapping(payload.get("user"))
    return {
        "state": payload.get("state") or "open",
        "merged": bool(payload.get("merged") or payload.get("merged_at")),
        "mergeable": payload.get("mergeable"),
        "head_sha": head.get("sha") or "",
        "head_ref": head.get("ref") or "",
        "head_repo_full_name": head_repo.get("full_name") or "",
        "base_sha": base.get("sha") or "",
        "base_ref": base.get("ref") or "",
        "author_login": author.get("login") or "",
        "html_url": payload.get("html_url"),
        "updated_at": payload.get("updated_at"),
    }


def _try_run_text(args, runner):
    try:
        completed = runner(
            args,
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout


def _items(payload, key):
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _normalize_failure_text(text):
    without_ansi = ANSI_ESCAPE_RE.sub("", text or "")
    redacted_result = redaction.redact_text(without_ansi)
    if not redacted_result["ok"]:
        return ""
    normalized = TIMESTAMP_RE.sub("", redacted_result["text"])
    lines = [
        WHITESPACE_RE.sub(" ", line).strip()
        for line in normalized.splitlines()
    ]
    return "\n".join(line for line in lines if line)


def failure_signature(failures):
    normalized = []
    for failure in sorted(
        failures,
        key=lambda item: (
            str(item.get("check_name") or ""),
            str(item.get("failure_summary") or ""),
        ),
    ):
        normalized.append(
            {
                "check_name": str(failure.get("check_name") or ""),
                "failure_summary": _normalize_failure_text(
                    failure.get("failure_summary") or ""
                ),
            }
        )
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence(text, max_summary_chars):
    clean_text = ANSI_ESCAPE_RE.sub("", text or "").strip()
    if not clean_text:
        return {
            "evidence_status": "unavailable",
            "failure_summary": "",
            "failure_signature": "",
        }
    result = redaction.redact_and_truncate(
        clean_text,
        max_chars=max_summary_chars,
    )
    if not result["ok"]:
        return {
            "evidence_status": result["status"],
            "failure_summary": "",
            "failure_signature": "",
        }
    summary = result["text"].strip()
    if not summary:
        return {
            "evidence_status": "unavailable",
            "failure_summary": "",
            "failure_signature": "",
        }
    payload = {"check_name": "", "failure_summary": summary}
    return {
        "evidence_status": "available",
        "failure_summary": summary,
        "failure_signature": failure_signature([payload]),
        "failure_summary_truncated": result["truncated"],
    }


def _workflow_observation(
    repo_full_name,
    workflow,
    *,
    max_summary_chars,
    runner,
):
    check_name = str(workflow.get("name") or "")
    conclusion = workflow.get("conclusion")
    evidence = {}
    if conclusion == "failure":
        jobs_payload = discover._try_run_json(
            [
                "gh",
                "api",
                f"repos/{repo_full_name}/actions/runs/{workflow.get('id')}/jobs?per_page=100",
            ],
            runner=runner,
        )
        failed_jobs = [
            job
            for job in _items(jobs_payload, "jobs")
            if job.get("conclusion") == "failure"
        ]
        excerpts = []
        evidence_status = "available"
        for job in failed_jobs:
            log = _try_run_text(
                [
                    "gh",
                    "api",
                    f"repos/{repo_full_name}/actions/jobs/{job.get('id')}/logs",
                ],
                runner,
            )
            if log is None:
                evidence_status = "unavailable"
                continue
            excerpts.append(f"[job: {job.get('name') or job.get('id')}]\n{log}")
        evidence = _evidence("\n".join(excerpts), max_summary_chars)
        if evidence_status == "unavailable" and not excerpts:
            evidence["evidence_status"] = "unavailable"
    observation = {
        "source_kind": "workflow_run",
        "external_id": str(workflow.get("id") or ""),
        "attempt_no": int(workflow.get("run_attempt") or 1),
        "check_name": check_name,
        "status": workflow.get("status") or "",
        "conclusion": conclusion,
        "details_url": workflow.get("html_url"),
        "completed_at": workflow.get("updated_at"),
        "freshness_at": workflow.get("updated_at"),
    }
    if evidence:
        evidence["failure_signature"] = failure_signature(
            [
                {
                    "check_name": check_name,
                    "failure_summary": evidence.get("failure_summary") or "",
                }
            ]
        ) if evidence.get("failure_summary") else ""
        observation.update(evidence)
    return observation


def _annotation_text(annotations):
    lines = []
    if not isinstance(annotations, list):
        return ""
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        location = str(annotation.get("path") or "")
        if annotation.get("start_line"):
            location = f"{location}:{annotation['start_line']}"
        message = str(annotation.get("message") or "")
        line = f"{location} {message}".strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _check_observation(
    repo_full_name,
    check,
    *,
    max_summary_chars,
    runner,
):
    check_name = str(check.get("name") or "")
    conclusion = check.get("conclusion")
    evidence = {}
    if conclusion == "failure":
        output = _mapping(check.get("output"))
        annotations = discover._try_run_json(
            [
                "gh",
                "api",
                f"repos/{repo_full_name}/check-runs/{check.get('id')}/annotations?per_page=100",
            ],
            runner=runner,
        )
        text = "\n".join(
            part
            for part in [
                str(output.get("title") or ""),
                str(output.get("summary") or ""),
                str(output.get("text") or ""),
                _annotation_text(annotations),
            ]
            if part
        )
        evidence = _evidence(text, max_summary_chars)
    observation = {
        "source_kind": "check_run",
        "external_id": str(check.get("id") or ""),
        "attempt_no": 1,
        "check_name": check_name,
        "status": check.get("status") or "",
        "conclusion": conclusion,
        "details_url": check.get("details_url"),
        "completed_at": check.get("completed_at"),
        "freshness_at": check.get("completed_at") or check.get("started_at"),
    }
    if evidence:
        evidence["failure_signature"] = failure_signature(
            [
                {
                    "check_name": check_name,
                    "failure_summary": evidence.get("failure_summary") or "",
                }
            ]
        ) if evidence.get("failure_summary") else ""
        observation.update(evidence)
    return observation


def collect_ci_observations(
    repo_full_name,
    head_sha,
    *,
    check_allowlist,
    max_summary_chars,
    runner=subprocess.run,
):
    allowed = set(check_allowlist)
    workflow_payload = discover._try_run_json(
        [
            "gh",
            "api",
            f"repos/{repo_full_name}/actions/runs?head_sha={head_sha}&per_page=100",
        ],
        runner=runner,
    )
    observations = [
        _workflow_observation(
            repo_full_name,
            workflow,
            max_summary_chars=max_summary_chars,
            runner=runner,
        )
        for workflow in _items(workflow_payload, "workflow_runs")
        if workflow.get("head_sha") == head_sha
        and workflow.get("name") in allowed
    ]

    checks_payload = discover._try_run_json(
        [
            "gh",
            "api",
            f"repos/{repo_full_name}/commits/{head_sha}/check-runs?per_page=100",
        ],
        runner=runner,
    )
    observations.extend(
        _check_observation(
            repo_full_name,
            check,
            max_summary_chars=max_summary_chars,
            runner=runner,
        )
        for check in _items(checks_payload, "check_runs")
        if check.get("name") in allowed
        and _mapping(check.get("app")).get("slug") != "github-actions"
    )
    return observations


def _observation_freshness(observation):
    raw_time = observation.get("freshness_at") or observation.get("completed_at")
    try:
        observed_at = datetime.fromisoformat(
            str(raw_time).replace("Z", "+00:00")
        )
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_at = observed_at.astimezone(timezone.utc)
    except (TypeError, ValueError):
        observed_at = datetime.min.replace(tzinfo=timezone.utc)
    return (
        observed_at,
        int(observation.get("attempt_no") or 1),
        str(observation.get("external_id") or ""),
    )


def summarize_ci(observations, check_allowlist):
    latest = {}
    for observation in observations:
        check_name = observation.get("check_name")
        if check_name not in check_allowlist:
            continue
        current = latest.get(check_name)
        if (
            current is None
            or _observation_freshness(observation)
            > _observation_freshness(current)
        ):
            latest[check_name] = observation

    missing = [name for name in check_allowlist if name not in latest]
    incomplete = [
        name
        for name, observation in latest.items()
        if observation.get("status") != "completed"
    ]
    if missing or incomplete:
        return {
            "status": "waiting",
            "missing_checks": missing,
            "incomplete_checks": sorted(incomplete),
            "observations": list(latest.values()),
        }

    manual = sorted(
        {
            str(observation.get("conclusion") or "")
            for observation in latest.values()
            if observation.get("conclusion") in MANUAL_CONCLUSIONS
        }
    )
    if manual:
        return {
            "status": "manual",
            "reason": "manual_conclusion",
            "manual_conclusions": manual,
            "observations": list(latest.values()),
        }

    failures = [
        observation
        for observation in latest.values()
        if observation.get("conclusion") == "failure"
    ]
    if failures:
        if any(
            failure.get("evidence_status") != "available"
            for failure in failures
        ):
            return {
                "status": "manual",
                "reason": "failure_evidence_unavailable",
                "manual_conclusions": [],
                "failures": failures,
                "observations": list(latest.values()),
            }
        signature = (
            failures[0].get("failure_signature")
            if len(failures) == 1
            else failure_signature(failures)
        )
        return {
            "status": "failing",
            "failures": failures,
            "failure_signature": signature,
            "observations": list(latest.values()),
        }

    if all(
        observation.get("conclusion") in SUCCESSFUL_CONCLUSIONS
        for observation in latest.values()
    ):
        return {
            "status": "green",
            "observations": list(latest.values()),
        }
    return {
        "status": "manual",
        "reason": "unsupported_conclusion",
        "manual_conclusions": sorted(
            {
                str(observation.get("conclusion") or "")
                for observation in latest.values()
            }
        ),
        "observations": list(latest.values()),
    }

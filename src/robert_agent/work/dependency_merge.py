"""Explicit operator authority to answer one question after a verified PR merge."""

import json
import re
from robert_agent.adapters.github import client, recording, sources
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence import records
from robert_agent.persistence.queries import dependency_merge as queries
from robert_agent.persistence.queries import github_scan_reads
from robert_agent.work.errors import WorkItemConflictError, WorkItemValidationError


_PR_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]{0,17})")


def validate(value, *, repo, task, question, expected_event_id, context):
    if context.actor_kind != "operator" or not context.actor_identity:
        raise WorkItemValidationError("Dependency wakeup requires explicit operator authority.")
    if not isinstance(value, dict) or set(value) != {"pr_url", "task_id", "task_updated_at"}:
        raise WorkItemValidationError("dependency requires pr_url, task_id and task_updated_at")
    url = value["pr_url"]
    match = _PR_URL.fullmatch(url) if isinstance(url, str) else None
    if not match or match[1].casefold() != repo.casefold():
        raise WorkItemValidationError("Dependency must be a canonical same-repository GitHub PR URL.")
    # Cache keys use the configured repository's spelling.
    value = {**value, "pr_url": f"https://github.com/{repo}/pull/{match[2]}"}
    if (
        not task
        or task["task_id"] != value["task_id"]
        or task["updated_at"] != value["task_updated_at"]
        or task["lifecycle"] != "waiting_for_user"
        or not question
        or question["event_type"] != "operator_question"
        or question["event_id"] != expected_event_id
    ):
        raise WorkItemConflictError("The dependency question or waiting task changed.")
    metadata = json.loads(question["metadata_json"])
    if metadata.get("task_id") != task["task_id"] or metadata.get("kind") == "supervisor_question":
        raise WorkItemConflictError("Dependency wakeup requires the original waiting task question.")
    return dict(value)


def merged(conn, repo_id, url):
    match = _PR_URL.fullmatch(url)
    if not match:
        return False
    repo, number = match.groups()
    responses = {}
    for kind in ("issues", "pulls"):
        key = client.command_key(["gh", "api", f"repos/{repo}/{kind}/{number}"])
        row = records.first(github_scan_reads.find_for_source_read(conn, repo_id, key, 0))
        if row:
            responses[key] = row[0]
    remote = sources.read_source_state(
        repo, "pull_request", int(number), recording.ReplayRunner(responses), require_explicit_state=True
    )
    return merge_proven(remote, url)


def merge_proven(remote, url):
    """Recognize a same-target merge in a trusted source-state observation."""
    return bool(
        remote
        and remote["state"] == "closed"
        and str(remote["html_url"] or "").casefold() == url.casefold()
        and remote["merged"]
        and utc_timestamp(remote["merged_at"]) is not None
    )


def pending(conn, repo_id, *, work_item_id=None):
    return queries.registrations(conn, repo_id, work_item_id=work_item_id)


def has_ready_reply(conn, repo_id):
    return any(
        merged(conn, repo_id, json.loads(registration["metadata_json"])["dependency"]["pr_url"])
        for registration in pending(conn, repo_id)
    )

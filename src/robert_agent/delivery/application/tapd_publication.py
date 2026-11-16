"""Check final GitHub content for identifiers of the originating TAPD ticket."""

from html import unescape
import re
import subprocess
from urllib.parse import unquote

from robert_agent.foundation import tapd_identity
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.delivery import GitHubActionType

_TICKET_PATH = re.compile(r"/(?:prong/stories|bugtrace/bugs)/view/", re.I)
_GIT_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _identifiers(ticket, config):
    projects = [tuple(ticket["project_key"].split("/", 1))]
    projects.extend((project["host"], project["workspace_id"]) for project in (config or {}).get("projects", []))
    hosts, workspaces = zip(*projects)
    numeric = "|".join(re.escape(value) for value in {*workspaces, ticket["external_id"]})
    return {host.casefold() for host in hosts}, re.compile(rf"(?<![a-z0-9])(?:{numeric})(?![a-z0-9])", re.I)


def check(actions, scope, ticket, config, *, run_command):
    """Inspect final action fields and the pinned commit range without retaining their text."""
    hosts, numbers = _identifiers(ticket, config)

    def contains_identifier(text):
        if not isinstance(text, str):
            return False
        text = unescape(unquote(text)).casefold()
        return bool(
            "tapd:" in text
            or "tapd-comment:" in text
            or _TICKET_PATH.search(text)
            or any(host in text for host in hosts)
            or numbers.search(text)
        )

    push = False
    for action in actions:
        kind = action.get("type")
        if kind == GitHubActionType.PUSH_EXISTING_PR:
            push = True
        if kind == GitHubActionType.COMMENT and tapd_identity.parse_ticket_url(action.get("target_url")):
            continue
        if any(contains_identifier(action.get(field)) for field in ("title", "body", "head", "base", "branch")):
            return "tapd_identifier_in_publication"
    if not push:
        return None
    head = scope.get("result_head_sha") or ""
    boundary = scope.get("observed_base_sha") or f"refs/remotes/upstream/{scope.get('base_branch', '')}"
    worktree = scope.get("worktree_path")
    if not worktree or not _GIT_OID.fullmatch(head):
        return "tapd_commit_range_unavailable"
    try:
        completed = run_command(
            ["git", "--no-replace-objects", "log", "--format=%B", head, f"^{boundary}", "--"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except ControlDeadlineExceeded:
        raise
    except (OSError, subprocess.CalledProcessError):
        return "tapd_commit_range_unavailable"
    if completed.returncode != 0:
        return "tapd_commit_range_unavailable"
    return "tapd_identifier_in_commit" if contains_identifier(completed.stdout) else None

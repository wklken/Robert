"""Remote evidence required to deliver a reviewed new PR."""

import hashlib
import subprocess

from robert_agent.adapters.git.remotes import github_repository
from robert_agent.adapters.git.review_revision import git, remote_branch_sha
from robert_agent.delivery import values
from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.persistence.queries import github_actions


def verify_branch(scope, remote, branch, expected, run_command):
    try:
        return remote_branch_sha(scope["worktree_path"], remote, branch, runner=run_command) == expected
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def base_is_current(scope, run_command):
    proof = scope["pre_pr_review"]
    try:
        upstream = git(scope["worktree_path"], "remote", "get-url", "upstream", runner=run_command)
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if github_repository(upstream) != scope["repo"].casefold():
        return False
    return verify_branch(scope, upstream, scope["base_branch"], proof["base_sha"], run_command)


def delivered_head(conn, action, scope):
    return any(
        kind == "push_existing_pr"
        and values.decode_json(receipt).get("result_head_sha") == scope["pre_pr_review"]["head_sha"]
        for _, kind, receipt in github_actions.find_for_delivery(conn, action["result_id"])
    )


def matches_remote_pr(scope, metadata, number, run_command):
    body = metadata.get("body")
    if (
        not str(number).isdigit()
        or not isinstance(body, str)
        or hashlib.sha256(body.encode()).hexdigest() != scope["pre_pr_review"].get("body_sha256")
    ):
        return False
    try:
        completed = run_command(["gh", "api", f"repos/{scope['repo']}/pulls/{number}"], capture_output=True, text=True)
    except ControlDeadlineExceeded:
        raise
    except (OSError, subprocess.SubprocessError):
        return False
    detail = values.decode_json(completed.stdout) if completed.returncode == 0 else {}
    head = detail.get("head")
    base = detail.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return False
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    proof = scope["pre_pr_review"]
    return (
        str(detail.get("number")) == str(number)
        and head.get("sha") == proof["head_sha"]
        and base.get("sha") == proof["base_sha"]
        and base.get("ref") == scope["base_branch"]
        and head.get("ref") == scope["branch_name"]
        and isinstance(head_repo, dict)
        and str(head_repo.get("full_name", "")).casefold()
        == f"{scope.get('head_owner') or scope['repo'].split('/')[0]}/{scope['repo'].split('/')[1]}".casefold()
        and isinstance(base_repo, dict)
        and str(base_repo.get("full_name", "")).casefold() == scope["repo"].casefold()
        and detail.get("body") == metadata.get("body")
    )

"""Select the assigned workspace for an attempt."""

import re

from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, SourceProvider

from robert_agent.adapters.git import workspace as worktree

_PUBLIC_REF = re.compile(r"rbt-[0-9a-z]{6,32}")
# These modes fetch pull/<number>/head, so they need the pull request number itself.
_PULL_REQUEST_MODES = frozenset({"existing_pr", "review_pr"})


def source_reference(event):
    """Name local workspaces and branches; TAPD sources have no number and use their public reference."""
    if event.get("provider") != SourceProvider.TAPD:
        return event["number"]
    public_ref = event.get("public_ref")
    if not isinstance(public_ref, str) or not _PUBLIC_REF.fullmatch(public_ref):
        raise ValueError("a TAPD source needs a valid public reference")
    return public_ref


def prepare_worktree(repo, event, route_result, dry_run):
    if route_result.get("native_review_workers") or route_result.get("native_vote_workers"):
        # The native coordinator fetches and pins this round after admission.
        return None
    skill_status = route_result.get("skill_status")
    if skill_status and not skill_status.get("runnable", True):
        return None
    workspace_mode = route_result.get("workspace_mode")
    if not workspace_mode:
        workspace_mode = (
            "review_pr"
            if route_result.get("worktree_mode") == "review_pr"
            else ("new_branch" if route_result.get("needs_worktree") else "none")
        )
    if workspace_mode == "none":
        return None
    if workspace_mode == "analysis" and not route_result.get("enforce_workspace_policy", False):
        return None
    if event.get("task_kind") in REMEDIATION_TASK_KINDS:
        return worktree.prepare_existing_pr_worktree(
            repo_root=repo["repo_root"],
            worktree_root=repo["worktree_root"],
            source_number=event["number"],
            head_branch=event["existing_pr_head_branch"],
            head_sha=event["head_sha"],
            base_branch=event.get("base_branch") or repo["default_base_branch"],
            base_sha=event["base_sha"],
            dry_run=dry_run,
        )
    return worktree.resolve_task_workspace(
        repo,
        {**route_result, "workspace_mode": workspace_mode},
        {
            "number": (
                event.get("existing_pr_number") or event["number"]
                if workspace_mode in _PULL_REQUEST_MODES
                else source_reference(event)
            ),
            "branch_slug": (event.get("branch_slug") or event.get("title") or "task"),
            "head_branch": event.get("existing_pr_head_branch"),
        },
        dry_run,
    )

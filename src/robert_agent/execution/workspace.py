"""Select the assigned workspace for an attempt."""

import re
import hashlib

from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS, SourceProvider

from robert_agent.adapters.git import workspace as worktree

_PUBLIC_REF = re.compile(r"rbt-[0-9a-z]{6,32}")
# These modes fetch pull/<number>/head, so they need the pull request number itself.
_PULL_REQUEST_MODES = frozenset({"existing_pr", "review_pr"})


def prepare_delivery_continuation(repo, saved, dry_run):
    """Preserve a completed delivery attempt's owned edits after a user reply."""
    path, branch = saved["worktree_path"], saved["branch_name"]
    if not path or not branch:
        return {"ok": False, "status": "blocked_unavailable_worktree"}
    error = worktree.check_worktree_reuse(
        repo["repo_root"], repo["worktree_root"], path, dry_run=dry_run, branch_name=branch, allow_dirty=True
    )
    return {
        "ok": not error,
        "status": error or "reused",
        "mode": "delivery_continuation",
        "worktree_path": path,
        "branch_name": branch,
        "base_branch": saved.get("base_branch") or repo["default_base_branch"],
        "continuation": {
            "previous_task_id": saved["task_id"],
            "previous_attempt_id": saved["previous_attempt_id"],
            "strategy": "resume_from_worktree",
            "handoff": saved["handoff"],
            "operator_question": saved["operator_question"],
            "reason_code": "delivery_question_response",
        },
    }


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
    if event.get("task_kind") in REMEDIATION_TASK_KINDS or (
        workspace_mode == "existing_pr" and (event.get("metadata") or {}).get("pin_pr_snapshot") is True
    ):
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
                else (
                    source_reference(event) + "-" + hashlib.sha256(event["event_fingerprint"].encode()).hexdigest()[:12]
                    if event.get("backport")
                    else source_reference(event)
                )
            ),
            "branch_slug": (event.get("branch_slug") or event.get("title") or "task"),
            "head_branch": event.get("existing_pr_head_branch"),
            "base_branch": event.get("base_branch") or repo["default_base_branch"],
        },
        dry_run,
    )

"""Select the assigned workspace for an attempt."""

from robert_agent.foundation.contracts.work import REMEDIATION_TASK_KINDS

from pathlib import Path
import subprocess

from robert_agent.foundation import commands

from robert_agent.adapters.git import native_publication, workspace as worktree


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
            "number": event["number"],
            "branch_slug": (event.get("branch_slug") or event.get("title") or "task"),
            "head_branch": event.get("existing_pr_head_branch"),
        },
        dry_run,
    )


def native_repository_paths(repo):
    """Resolve configured aliases at preparation and pin their canonical paths."""
    root = Path(repo["repo_root"]).resolve(strict=True)
    worktree_root = Path(repo["worktree_root"]).resolve()
    if not root.is_dir():
        raise ValueError("native repository root is unavailable")
    return str(root), str(worktree_root)


def prepare_source_worktree(repo, purpose, *, pr=None):
    """Materialize only the exact controller-owned workspace purpose."""
    if purpose["mode"] == "readonly":
        return {"ok": True, "worktree_path": repo["repo_root"], "branch_name": None}
    if purpose["mode"] == "new_branch":
        return worktree.plan_worktree(
            repo["repo_root"],
            repo["worktree_root"],
            purpose["workspace_key"],
            "source-work",
            repo["default_base_branch"],
            dry_run=False,
        )
    if purpose["mode"] != "existing_pr" or not pr:
        raise ValueError("invalid native workspace purpose")
    return worktree.prepare_existing_pr_worktree(
        repo["repo_root"],
        repo["worktree_root"],
        source_number=pr["number"],
        head_branch=pr["branch_name"],
        head_sha=pr["head_sha"],
        base_branch=pr["base_branch"],
        base_sha=pr["base_sha"],
        dry_run=False,
    )


def validate_source_workspace(repo, purpose, worktree_result, *, require_git_pin=True):
    """A prepared source workspace cannot fall back to a GitHub event lookup."""
    if not isinstance(purpose, dict) or not worktree_result:
        return False
    try:
        roots = native_repository_paths(repo)
        if roots != (purpose.get("repo_root"), purpose.get("worktree_root")):
            return False
        path = Path(worktree_result["worktree_path"])
        branch = worktree_result.get("branch_name")
        if purpose.get("mode") == "readonly":
            return str(path) == roots[0] and branch is None
        if (
            purpose.get("mode") not in {"new_branch", "existing_pr"}
            or not branch
            or branch != purpose.get("branch_name")
            or not path.resolve(strict=True).is_relative_to(roots[1])
            or worktree.check_worktree_reuse(roots[0], roots[1], path, branch_name=branch)
        ):
            return False
        if require_git_pin:
            return native_publication.verify_pin(path, purpose, repo["full_name"])
        return True
    except commands.ControlDeadlineExceeded:
        raise
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        return False

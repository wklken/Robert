#!/usr/bin/env python3
import argparse
import re
import shlex
import subprocess
from pathlib import Path

from robert_agent.common import emit


def _slug(value):
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return normalized or "task"


def _existing_worktree_for_branch(repo_root, branch_name):
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    worktree_path = None
    prefix = "branch refs/heads/"
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            worktree_path = Path(line.split(" ", 1)[1])
            continue
        if worktree_path and line.startswith(prefix) and line[len(prefix) :] == branch_name:
            return worktree_path
        if not line:
            worktree_path = None
    return None


def plan_worktree(
    repo_root,
    worktree_root,
    source_number,
    short_slug,
    base_branch,
    existing_pr_head_branch=None,
    dry_run=True,
):
    repo_root = Path(repo_root)
    worktree_root = Path(worktree_root)
    if existing_pr_head_branch:
        branch_name = existing_pr_head_branch
    else:
        branch_name = f"codex/dd-{source_number}-{_slug(short_slug)}"

    existing_worktree_path = _existing_worktree_for_branch(repo_root, branch_name)
    if existing_worktree_path:
        mode = "reuse_existing_worktree"
    elif existing_pr_head_branch:
        mode = "reuse_existing_pr"
    else:
        mode = "new_branch"

    worktree_path = existing_worktree_path or (worktree_root / branch_name.replace("/", "__"))
    start_point = f"upstream/{base_branch}"
    commands = []
    if mode == "new_branch":
        commands.append(f"git fetch upstream {base_branch}")
        commands.append(f"git worktree add {worktree_path} -b {branch_name} {start_point}")
    elif mode == "reuse_existing_worktree":
        commands.append(f"reuse existing worktree {worktree_path}")
    else:
        commands.append(f"git worktree add {worktree_path} {branch_name}")

    result = {
        "ok": True,
        "status": "planned" if dry_run else "created",
        "mode": mode,
        "repo_root": str(repo_root),
        "worktree_root": str(worktree_root),
        "worktree_path": str(worktree_path),
        "branch_name": branch_name,
        "base_branch": base_branch,
        "start_point": start_point,
        "commands": commands,
    }
    if dry_run:
        return result

    if mode == "reuse_existing_worktree":
        return result

    worktree_root.mkdir(parents=True, exist_ok=True)
    if mode == "new_branch":
        subprocess.run(["git", "fetch", "upstream", base_branch], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch_name, start_point],
            cwd=repo_root,
            check=True,
        )
    else:
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch_name],
            cwd=repo_root,
            check=True,
        )
    return result


def plan_review_worktree(
    repo_root,
    worktree_root,
    source_number,
    short_slug,
    base_branch,
    dry_run=True,
):
    repo_root = Path(repo_root)
    worktree_root = Path(worktree_root)
    branch_name = f"review/pr-{source_number}-{_slug(short_slug)}"
    existing_worktree_path = _existing_worktree_for_branch(repo_root, branch_name)
    mode = "reuse_existing_worktree" if existing_worktree_path else "review_pr"
    worktree_path = existing_worktree_path or (worktree_root / branch_name.replace("/", "__"))
    start_point = f"upstream/{base_branch}"
    commands = []
    if mode == "reuse_existing_worktree":
        commands.append(f"reuse existing worktree {worktree_path}")
    else:
        commands.append(f"git fetch upstream {base_branch}")
        commands.append(f"git fetch upstream pull/{source_number}/head:{branch_name}")
        commands.append(f"git worktree add {worktree_path} {branch_name}")

    result = {
        "ok": True,
        "status": "planned" if dry_run else "created",
        "mode": mode,
        "repo_root": str(repo_root),
        "worktree_root": str(worktree_root),
        "worktree_path": str(worktree_path),
        "branch_name": branch_name,
        "base_branch": base_branch,
        "start_point": start_point,
        "commands": commands,
    }
    if dry_run or mode == "reuse_existing_worktree":
        return result

    worktree_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "fetch", "upstream", base_branch], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "fetch", "upstream", f"pull/{source_number}/head:{branch_name}"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", str(worktree_path), branch_name],
        cwd=repo_root,
        check=True,
    )
    return result


def _registered_worktree(repo_root, expected_path):
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    expected_path = Path(expected_path).resolve()
    return any(
        line.startswith("worktree ")
        and Path(line.split(" ", 1)[1]).resolve() == expected_path
        for line in completed.stdout.splitlines()
    )


def plan_analysis_worktree(
    repo_root,
    worktree_root,
    source_number,
    base_branch,
    dry_run=True,
):
    repo_root = Path(repo_root)
    worktree_root = Path(worktree_root)
    worktree_path = worktree_root / f"analysis-{source_number}"
    commands = [
        ["git", "fetch", "upstream", base_branch],
        [
            "git",
            "worktree",
            "add",
            "--detach",
            str(worktree_path),
            f"upstream/{base_branch}",
        ],
    ]
    reused = _registered_worktree(repo_root, worktree_path)
    if not dry_run and not reused:
        worktree_root.mkdir(parents=True, exist_ok=True)
        for command in commands:
            subprocess.run(command, cwd=repo_root, check=True)
    return {
        "ok": True,
        "status": (
            "reused"
            if reused
            else ("planned" if dry_run else "prepared")
        ),
        "mode": "analysis",
        "worktree_path": str(worktree_path),
        "branch_name": None,
        "commands": [
            " ".join(shlex.quote(part) for part in command)
            for command in commands
        ],
    }


def resolve_task_workspace(repo, route, source, dry_run):
    mode = route["workspace_mode"]
    if mode == "none":
        return {
            "ok": True,
            "mode": "none",
            "worktree_path": source["artifact_dir"],
            "branch_name": None,
        }
    if mode == "new_branch":
        return plan_worktree(
            repo_root=repo["repo_root"],
            worktree_root=repo["worktree_root"],
            source_number=source["number"],
            short_slug=source["branch_slug"],
            base_branch=repo["default_base_branch"],
            dry_run=dry_run,
        )
    if mode == "existing_pr":
        return plan_worktree(
            repo_root=repo["repo_root"],
            worktree_root=repo["worktree_root"],
            source_number=source["number"],
            short_slug=source["branch_slug"],
            base_branch=repo["default_base_branch"],
            existing_pr_head_branch=source["head_branch"],
            dry_run=dry_run,
        )
    if mode == "review_pr":
        return plan_review_worktree(
            repo_root=repo["repo_root"],
            worktree_root=repo["worktree_root"],
            source_number=source["number"],
            short_slug=source["branch_slug"],
            base_branch=repo["default_base_branch"],
            dry_run=dry_run,
        )
    if mode == "analysis":
        return plan_analysis_worktree(
            repo_root=repo["repo_root"],
            worktree_root=repo["worktree_root"],
            source_number=source["number"],
            base_branch=repo["default_base_branch"],
            dry_run=dry_run,
        )
    raise ValueError(f"unsupported workspace_mode: {mode}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--worktree-root", required=True)
    parser.add_argument("--source-number", required=True, type=int)
    parser.add_argument("--short-slug", required=True)
    parser.add_argument("--base-branch", required=True)
    parser.add_argument("--existing-pr-head-branch")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = plan_worktree(
        repo_root=args.repo_root,
        worktree_root=args.worktree_root,
        source_number=args.source_number,
        short_slug=args.short_slug,
        base_branch=args.base_branch,
        existing_pr_head_branch=args.existing_pr_head_branch,
        dry_run=args.dry_run,
    )
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())

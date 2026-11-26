"""Git observations and local workspace operations for pre-publication review."""

import re
import subprocess


def git(path, *args, runner=subprocess.run):
    command = ["git", *args]
    result = runner(command, cwd=path, check=True, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.stdout.strip()


def remote_branch_sha(path, remote, branch, *, runner=subprocess.run):
    ref = "refs/heads/" + branch
    fields = git(path, "ls-remote", "--exit-code", remote, ref, runner=runner).split()
    if len(fields) != 2 or fields[1] != ref or len(fields[0]) not in {40, 64}:
        raise ValueError("cannot verify remote branch revision")
    try:
        int(fields[0], 16)
    except ValueError:
        raise ValueError("invalid remote branch object id") from None
    return fields[0]


def worktree_state(path, *, read_git=git):
    """Read the current commit and dirtiness from the same Git observation."""
    status = read_git(
        path,
        "status",
        "--porcelain=v2",
        "--branch",
        "--no-ahead-behind",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    heads = []
    dirty = False
    for line in status.splitlines():
        if line.startswith("# branch.oid "):
            heads.append(line.removeprefix("# branch.oid "))
        elif line and not line.startswith("# "):
            dirty = True
    if len(heads) != 1 or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", heads[0]):
        raise ValueError("Git status did not report a committed HEAD")
    return heads[0], dirty


def clean_head(path, branch=None):
    if branch is not None and git(path, "symbolic-ref", "--short", "HEAD") != branch:
        raise ValueError("assigned branch changed")
    head, dirty = worktree_state(path)
    if dirty:
        raise ValueError("review delivery requires a clean worktree")
    return head

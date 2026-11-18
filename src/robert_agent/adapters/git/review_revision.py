"""Git observations and local workspace operations for pre-publication review."""

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


def clean_head(path, branch=None):
    if branch is not None and git(path, "symbolic-ref", "--short", "HEAD") != branch:
        raise ValueError("assigned branch changed")
    if git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        raise ValueError("review delivery requires a clean worktree")
    return git(path, "rev-parse", "HEAD")

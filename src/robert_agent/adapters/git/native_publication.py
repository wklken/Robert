"""Observe native publication facts from a bound local Git worktree."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryFile

from robert_agent.adapters.git.remotes import github_repository
from robert_agent.foundation.commands import run_command

_SHA = re.compile(r"[0-9a-f]{40}")
_IDENT = re.compile(rb"(.+ <[^<>\r\n]+>) [0-9]+ [+-][0-9]{4}")
_MODE = {"A": "added", "M": "modified", "D": "deleted"}
_MAX_OUTPUT = 256 * 1024
_MAX_COMMITS = 100
_MAX_CHANGES = 200


@dataclass(frozen=True, slots=True)
class CodeFacts:
    head: str
    commits: tuple[str, ...]
    changes: tuple[tuple[str, str], ...]


def _git(path, *arguments, accept=(0,)):
    # Use configured Git identity and effective remotes, but ignore inherited
    # repository, config and author override variables and replace refs.
    environment = {key: val for key, val in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="",
    )
    with TemporaryFile() as output:
        result = run_command(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        output.seek(0)
        data = output.read(_MAX_OUTPUT + 1)
    if result.returncode not in accept or len(data) > _MAX_OUTPUT:
        raise ValueError("Native Git inspection unavailable")
    return data, result.returncode


def _line(path, *arguments):
    raw, _ = _git(path, *arguments)
    text = raw.decode("utf-8").rstrip("\n")
    if not text or "\n" in text or "\r" in text:
        raise ValueError("Unexpected native Git fact")
    return text


def _sha(path, ref):
    token = _line(path, "rev-parse", "--verify", ref + "^{commit}")
    if not _SHA.fullmatch(token):
        raise ValueError("Unsupported Git object identity")
    return token


def _remote(path, name, *, push=False):
    options = ("--push",) if push else ()
    url = _line(path, "remote", "get-url", *options, "--all", name)
    repo = github_repository(url)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+", repo)
        or github_repository(_line(path, "ls-remote", "--get-url", url)) != repo
    ):
        raise ValueError("Native GitHub remote unavailable")
    return repo


def _worktree(path, purpose):
    root = Path(purpose["repo_root"])
    parent = Path(purpose["worktree_root"])
    tree = Path(path)
    if (
        root != root.resolve(strict=True)
        or parent != parent.resolve(strict=True)
        or tree != tree.resolve(strict=True)
        or parent == tree
        or not tree.is_relative_to(parent)
        or _line(tree, "rev-parse", "--show-toplevel") != str(tree)
    ):
        raise ValueError("Native Git worktree changed")
    common = (tree / _line(tree, "rev-parse", "--git-common-dir")).resolve(strict=True)
    if common != (root / _line(root, "rev-parse", "--git-common-dir")).resolve(strict=True):
        raise ValueError("Native Git worktree repository changed")
    if (common / "info" / "grafts").exists() or _line(tree, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Native Git ancestry unavailable")
    branch = purpose["branch_name"]
    if purpose["mode"] == "new_branch":
        key = purpose["workspace_key"]
        if not re.fullmatch(r"[0-9a-f]{32}", key) or branch != f"codex/dd-{key}-source-work":
            raise ValueError("Unbound native branch")
    elif purpose["mode"] != "existing_pr" or branch != purpose["pr"]["branch_name"]:
        raise ValueError("Unbound native branch")
    if _line(tree, "symbolic-ref", "--quiet", "HEAD") != f"refs/heads/{branch}":
        raise ValueError("Native Git branch changed")
    if _git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=normal")[0]:
        raise ValueError("Native Git worktree is dirty")
    return tree


def _identity(tree, kind):
    data = _line(tree, "-c", "user.useConfigOnly=true", "var", f"GIT_{kind}_IDENT").encode("utf-8")
    match = _IDENT.fullmatch(data)
    # git var may fall back to the system account when user identity is absent.
    if not match:
        raise ValueError("Configured Git identity unavailable")
    return match.group(1).decode("utf-8")


def verify_upstream(path, repository):
    """Check the effective local upstream configuration without network access."""
    if _remote(path, "upstream") != repository.casefold():
        raise ValueError("Native GitHub upstream changed")


def readonly_snapshot(path, repository):
    """Pin a clean mapped checkout for later comparison; this is not file isolation."""
    tree = Path(path)
    if tree != tree.resolve(strict=True) or _line(tree, "rev-parse", "--show-toplevel") != str(tree):
        raise ValueError("Readonly repository root changed")
    common = (tree / _line(tree, "rev-parse", "--git-common-dir")).resolve(strict=True)
    if (common / "info" / "grafts").exists() or _line(tree, "rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("Readonly repository history unavailable")
    verify_upstream(tree, repository)
    head = _sha(tree, "HEAD")
    entries = _git(tree, "ls-files", "-v", "-z")[0].split(b"\0")
    if any(entry and not entry.startswith(b"H ") for entry in entries):
        raise ValueError("Readonly index can hide file changes")
    if _git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=all")[0]:
        raise ValueError("Readonly repository is dirty")
    if _sha(tree, "HEAD") != head:
        raise ValueError("Readonly repository changed during observation")
    return {"version": 1, "repository": repository.casefold(), "root": str(tree), "common": str(common), "head": head}


def pin(path, purpose, repository, base_branch):
    """Freeze the initial objects, push repository and configured Git identities."""
    tree = _worktree(path, purpose)
    initial = _sha(tree, "HEAD")
    repository = repository.casefold()
    destination = _remote(tree, "origin", push=True)
    verify_upstream(tree, repository)
    if purpose["mode"] == "existing_pr":
        pr = purpose["pr"]
        if (
            initial != pr["head_sha"]
            or base_branch != pr["base_branch"]
            or destination != pr["head_repository"].casefold()
            or not _SHA.fullmatch(pr["base_sha"])
            or _sha(tree, pr["base_sha"]) != pr["base_sha"]
        ):
            raise ValueError("Native PR snapshot changed")
        base = pr["base_sha"]
    else:
        if destination.split("/")[1] != repository.split("/")[1] or initial != _sha(
            tree, f"refs/remotes/upstream/{base_branch}"
        ):
            raise ValueError("Native base branch or push repository changed")
        base = initial
    return {
        "version": 1,
        "initial_head": initial,
        "base_sha": base,
        "base_branch": base_branch,
        "repository": repository,
        "push_repository": destination,
        "author": _identity(tree, "AUTHOR"),
        "committer": _identity(tree, "COMMITTER"),
    }


def verify_pin(path, purpose, repository):
    """Refuse a launch when its recorded local configuration or initial HEAD changed."""
    expected = purpose.get("git_pin")
    return isinstance(expected, dict) and expected == pin(path, purpose, repository, expected["base_branch"])


def _verify_commit(tree, sha, *, pin, branch_mode, message):
    raw, _ = _git(tree, "cat-file", "commit", sha)
    header, separator, body = raw.partition(b"\n\n")
    if not separator or body != (message + "\n").encode("utf-8"):
        raise ValueError("Unapproved native commit message")
    records = header.split(b"\n")
    if any(row.split(b" ", 1)[0] not in {b"tree", b"parent", b"author", b"committer"} for row in records):
        raise ValueError("Unapproved native commit headers")
    for field in ("author", "committer"):
        prefix = field.encode() + b" "
        matches = [row.removeprefix(prefix) for row in records if row.startswith(prefix)]
        value = _IDENT.fullmatch(matches[0]) if len(matches) == 1 else None
        if not value or value.group(1).decode("utf-8") != pin[field]:
            raise ValueError("Unapproved native commit identity")
    parents = [row.removeprefix(b"parent ").decode("ascii") for row in records if row.startswith(b"parent ")]
    if len(parents) == 1:
        return
    if (
        len(parents) != 2
        or branch_mode != "existing_pr"
        or parents[1] != pin["base_sha"]
        or _git(tree, "merge-base", "--is-ancestor", pin["initial_head"], parents[0], accept=(0, 1))[1] != 0
    ):
        raise ValueError("Unapproved native merge ancestry")


def inspect(path, purpose, binding, *, expected_head):
    """Read bounded commits and changed names using immutable objects only."""
    tree = _worktree(path, purpose)
    head = _sha(tree, "HEAD")
    expected_message = purpose.get("commit_message")
    initial, base = binding["initial_head"], binding["base_sha"]
    if (
        binding["version"] != 1
        or not expected_message
        or not all(_SHA.fullmatch(value) for value in (initial, base, head))
        or head == initial
        or head != expected_head
        or _remote(tree, "upstream") != binding["repository"]
        or _remote(tree, "origin", push=True) != binding["push_repository"]
        or _git(tree, "merge-base", "--is-ancestor", initial, head, accept=(0, 1))[1] != 0
    ):
        raise ValueError("Native Git publication scope changed")
    excluded = [initial]
    if purpose["mode"] == "existing_pr":
        pr = purpose["pr"]
        if (initial, base, binding["base_branch"], binding["push_repository"]) != (
            pr["head_sha"],
            pr["base_sha"],
            pr["base_branch"],
            pr["head_repository"].casefold(),
        ):
            raise ValueError("Native PR pin changed")
        excluded.append(base)
    elif base != initial:
        raise ValueError("Native base pin changed")
    raw, _ = _git(tree, "rev-list", f"--max-count={_MAX_COMMITS + 1}", head, "--not", *excluded)
    commits = tuple(raw.decode("ascii").splitlines())
    if not commits or len(commits) > _MAX_COMMITS or any(not _SHA.fullmatch(sha) for sha in commits):
        raise ValueError("Native Git commit range unavailable")
    for sha in commits:
        _verify_commit(tree, sha, pin=binding, branch_mode=purpose["mode"], message=expected_message)
    data, _ = _git(tree, "diff", "--name-status", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", initial, head)
    parts = data.decode("utf-8").split("\0")
    if parts[-1] != "" or len(parts) % 2 != 1 or len(parts) // 2 > _MAX_CHANGES:
        raise ValueError("Native Git change set unavailable")
    changes = []
    for status, name in zip(parts[:-1:2], parts[1::2], strict=True):
        if status not in _MODE:
            raise ValueError("Unsupported native Git change")
        changes.append((_MODE[status], name))
    if (
        not changes
        or _worktree(tree, purpose) != tree
        or _sha(tree, "HEAD") != head
        or _remote(tree, "origin", push=True) != binding["push_repository"]
        or _remote(tree, "upstream") != binding["repository"]
    ):
        raise ValueError("Native Git head or changes unavailable")
    return CodeFacts(head, commits, tuple(changes))


def mapped_files(root, paths, *, revision="HEAD"):
    """Confirm only exact regular-file paths in a controller-selected Git tree."""
    if not paths:
        return set()
    tree = Path(root)
    head = _sha(tree, revision)
    safe = sorted(
        name
        for name in paths
        if re.fullmatch(r"[A-Za-z0-9._/-]{1,240}", name)
        and all(part not in {"", ".", "..", ".git"} for part in name.split("/"))
    )
    if not safe:
        return set()
    raw, _ = _git(tree, "--literal-pathspecs", "ls-tree", "-z", head, "--", *safe)
    mapped = set()
    for line in raw.decode("utf-8").split("\0"):
        metadata, _, name = line.partition("\t")
        if name in safe and metadata.split()[:2] in (["100644", "blob"], ["100755", "blob"]):
            mapped.add(name)
    return mapped

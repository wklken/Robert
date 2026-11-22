"""Read selected blobs at a verified default-branch commit."""

from collections import Counter
from pathlib import PurePosixPath
import re

from robert_agent.adapters.git.remotes import github_repository
from robert_agent.foundation.commands import run_command
from robert_agent.security.text import safe_text


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,31}|[\u4e00-\u9fff]{2,12}")
_COMMON = frozenset(("the", "for", "and", "this", "that", "with", "from", "when", "have", "should", "bug", "fix"))
_SOURCE_SUFFIXES = frozenset(
    (
        ".py",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".vue",
        ".rb",
        ".c",
        ".cpp",
        ".h",
        ".cs",
        ".swift",
        ".sh",
        ".sql",
    )
)
_REMOTE = "upstream"
_REF = "refs/robert/proposer/default"


def _git(root, *args, accepted=(0,)):
    result = run_command(
        ["git", "--no-replace-objects", "-C", root, *args],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in accepted:
        raise ValueError("proposal Git evidence is unavailable")
    return result.stdout


def pinned_commit(repo):
    root, base = repo["repo_root"], repo["default_base_branch"]
    if not base or base.startswith("-"):
        raise ValueError("invalid proposal base branch")
    remote = _git(root, "remote", "get-url", _REMOTE).decode("utf-8").strip()
    remote_repository = github_repository(remote)
    if remote_repository and remote_repository != repo["full_name"].casefold():
        raise ValueError("proposal upstream does not match the configured repository")
    source = f"refs/heads/{base}"
    _git(root, "check-ref-format", source)
    # Use a dedicated ref. A concurrent worktree preparation may update FETCH_HEAD.
    _git(root, "fetch", "--no-write-fetch-head", "--no-tags", _REMOTE, f"+{source}:{_REF}")
    commit = _git(root, "rev-parse", "--verify", f"{_REF}^{{commit}}").decode("ascii").strip()
    if not _OID.fullmatch(commit):
        raise ValueError("invalid proposal commit")
    return commit


def excerpts(repo, commit, ticket, limit=8):
    if not _OID.fullmatch(commit):
        raise ValueError("invalid proposal commit")
    root = repo["repo_root"]
    terms = Counter(term.casefold() for term in _WORD.findall(ticket) if term.casefold() not in _COMMON)
    words = [term for term, _ in terms.most_common(16)]
    if not words:
        return []
    files = []
    tree = _git(root, "ls-tree", "-r", "-l", "-z", commit).split(b"\0")
    for entry in tree:
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, oid, size = header.split()
        path = raw_path.decode("utf-8", errors="replace")
        if (
            mode not in (b"100644", b"100755")
            or kind != b"blob"
            or not size.isdigit()
            or int(size) > 131072
            or "\ufffd" in path
            or PurePosixPath(path).suffix not in _SOURCE_SUFFIXES
        ):
            continue
        score = sum(term in path.casefold() for term in words)
        files.append((score, path, oid.decode("ascii")))
    # Match body text across tracked files without writing a worktree. The
    # approved list below excludes symlinks, large blobs and non-code assets.
    hits = _git(
        root,
        "grep",
        "--name-only",
        "-z",
        "--no-color",
        "-I",
        "-i",
        "-F",
        *(argument for word in words[:8] for argument in ("-e", word)),
        commit,
        "--",
        accepted=(0, 1),
    ).split(b"\0")
    prefix = (commit + ":").encode()
    matched = {hit.removeprefix(prefix).decode("utf-8", errors="replace") for hit in hits if hit.startswith(prefix)}
    files = sorted(files, key=lambda row: (-row[0], row[1] not in matched, row[1]))
    result = []
    for score, path, oid in files:
        if len(result) >= limit or (score == 0 and path not in matched):
            break
        blob = _git(root, "cat-file", "blob", oid).decode("utf-8", errors="replace")
        lines = blob.splitlines()
        if not lines:
            continue
        scores = [sum(word in line.casefold() for word in words) for line in lines]
        center = max(range(len(scores)), key=scores.__getitem__)
        start, end = max(0, center - 8), min(len(lines), center + 10)
        result.append(
            {
                "id": f"code-{len(result) + 1}",
                "path": path,
                "start": start + 1,
                "end": end,
                "text": "\n".join(safe_text(line, 160) for line in lines[start:end]),
            }
        )
    return result

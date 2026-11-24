"""Exact source observations and explicit incomplete deep-pin provenance."""

import hashlib
from pathlib import Path
import os
import stat

from robert_agent.adapters.git.review_revision import git
from robert_agent.adapters.git.source_files import untracked_source_files


class SourcePinUnavailable(ValueError):
    """Basic source identity is known, but cannot authorize recovery reuse."""

    def __init__(self, checkpoint, reason):
        super().__init__(reason)
        self.checkpoint = {**checkpoint, "source_pin_error": reason}


def observe_source(run, manifest, worktree):
    path = Path(worktree).absolute()
    if path.resolve() != path or not path.is_dir():
        raise ValueError("stage source worktree path changed")
    checkpoint = {
        "run_id": run["run_id"],
        "prompt_sha256": run["prompt_sha256"],
        "snapshot_id": manifest.get("snapshot_id"),
        "head_sha": manifest.get("head_sha"),
        "base_sha": manifest.get("base_sha"),
        "worktree_path": str(path),
    }
    if not manifest.get("head_sha") and not manifest.get("implementing"):
        checkpoint["context_files"] = dict(manifest.get("files") or {})
        return checkpoint
    checkpoint.update(
        worktree_head=git(path, "rev-parse", "HEAD"),
        branch=git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        source_status=git(path, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"),
        tracked_diff_sha256=hashlib.sha256(
            git(path, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv").encode()
        ).hexdigest(),
    )
    # Ignored local configuration can still affect a resumed model or its tools.
    # Keep that bounded pin while excluding ignored generated environments.
    untracked = untracked_source_files(path)
    if len(untracked) > 1000:
        raise SourcePinUnavailable(checkpoint, "stage source has too many untracked files to pin")
    hashes = {}
    try:
        for name in filter(None, untracked):
            file = path / name
            resolved = file.resolve()
            if not resolved.is_relative_to(path):
                raise SourcePinUnavailable(checkpoint, "stage source contains an untracked symlink")
            link = str(file.readlink()) if file.is_symlink() else None
            if resolved != file and link is None:
                raise SourcePinUnavailable(checkpoint, "stage source contains an untracked symlink directory")
            fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if (
                    not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                    or os.fstat(stream.fileno()).st_size > 16 * 1024 * 1024
                ):
                    raise SourcePinUnavailable(checkpoint, "stage source untracked file cannot be pinned")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                hashes[name] = (
                    hashlib.sha256(("symlink:" + link + "\0" + digest).encode()).hexdigest() if link else digest
                )
    except OSError as exc:
        raise SourcePinUnavailable(checkpoint, "stage source untracked file could not be read") from exc
    checkpoint["untracked_sha256"] = hashes
    return checkpoint

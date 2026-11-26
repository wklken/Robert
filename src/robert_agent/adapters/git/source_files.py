"""List local source inputs without walking ignored dependency installations."""

from robert_agent.adapters.git.review_revision import git


GENERATED_DIRECTORIES = (".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache", "__pycache__", ".ruff_cache")


def worktree_files(path, *, read_git=git):
    """List tracked paths and all untracked paths, including Git-ignored files."""
    tracked, untracked = [], []
    for entry in filter(None, read_git(path, "ls-files", "--cached", "--others", "-t", "-z").split("\0")):
        tag, _, name = entry.partition(" ")
        if not name or tag not in {"H", "M", "S", "?"}:
            raise ValueError("unexpected Git source listing")
        (untracked if tag == "?" else tracked).append(name)
    return tracked, untracked


def untracked_source_files(path):
    # Ignore rules alone never exempt configuration/source. Only generated
    # directories that Git also ignores are excluded from the deep source pin.
    unignored = git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    ignored = git(
        path,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        *(f":(glob,exclude)**/{name}/**" for name in GENERATED_DIRECTORIES),
    ).split("\0")
    return sorted(set(filter(None, (*unignored, *ignored))))

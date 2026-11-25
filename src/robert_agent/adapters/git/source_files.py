"""List local source inputs without walking ignored dependency installations."""

from robert_agent.adapters.git.review_revision import git


GENERATED_DIRECTORIES = (".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache", "__pycache__", ".ruff_cache")


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

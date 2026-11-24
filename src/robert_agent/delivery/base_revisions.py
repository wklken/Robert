"""Conservative evidence for a reviewed PR's documentation-only base advance."""

from pathlib import PurePosixPath
import subprocess

from robert_agent.adapters.git.review_revision import git
from robert_agent.foundation.commands import ControlDeadlineExceeded


def documentation_refresh(path, reviewed, current, final, *, runner=subprocess.run):
    """Return merge evidence only when the upstream change cannot alter runtime inputs."""
    try:
        git(path, "merge-base", "--is-ancestor", reviewed, current, runner=runner)
        merge_base = git(path, "merge-base", reviewed, final, runner=runner)
        if git(path, "merge-base", current, final, runner=runner) != merge_base:
            return None
        upstream = set(
            git(path, "diff", "--no-renames", "--name-only", reviewed, current, "--", runner=runner).splitlines()
        )
        changed = set(
            git(path, "diff", "--no-renames", "--name-only", merge_base, final, "--", runner=runner).splitlines()
        )
        if not upstream or upstream & changed:
            return None
        for name in upstream:
            item = PurePosixPath(name)
            if item.name == "AGENTS.md" or not (
                name in {"README.md", "CHANGELOG.md"} or name.startswith("docs/") and item.suffix in {".md", ".rst"}
            ):
                return None
        for line in git(path, "diff", "--raw", "--no-renames", reviewed, current, "--", runner=runner).splitlines():
            modes = line.split()[:2]
            if (
                len(modes) != 2
                or modes[0].removeprefix(":") not in {"000000", "100644"}
                or modes[1] not in {"000000", "100644"}
            ):
                return None
        tree = git(path, "merge-tree", "--write-tree", current, final, runner=runner)
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {
        "reviewed_base_sha": reviewed,
        "base_sha": current,
        "final_sha": final,
        "merge_base_sha": merge_base,
        "upstream_paths": sorted(upstream),
        "reviewed_paths": sorted(changed),
        "merge_tree_sha": tree,
    }

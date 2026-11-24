"""Conservative evidence for a reviewed PR's exact base advance."""

from pathlib import PurePosixPath
import subprocess

from robert_agent.adapters.git.review_revision import git
from robert_agent.delivery.base_dependencies import dependency_certificate, documentation_path
from robert_agent.delivery.mixed_base_dependencies import mixed_certificate
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


def compatibility_refresh(path, reviewed, current, final, *, round_id, snapshot_id, candidate, runner=subprocess.run):
    """Certify one frozen candidate against an exact current base without merging it."""
    proof = documentation_refresh(path, reviewed, current, final, runner=runner)
    if proof:
        proof = {**proof, "kind": "documentation"}
    else:
        proof = _source_refresh(path, reviewed, current, final, runner=runner)
        if not proof:
            proof = _source_refresh(path, reviewed, current, final, runner=runner, mixed=True)
    if not proof:
        return None
    try:
        git(path, "merge-base", "--is-ancestor", candidate, final, runner=runner)
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return {"version": 1, "round_id": round_id, "snapshot_id": snapshot_id, "candidate_sha": candidate, **proof}


def _source_refresh(path, reviewed, current, final, *, runner, mixed=False):
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
        if not upstream or not changed or upstream & changed:
            return None
        if mixed and any(PurePosixPath(name).suffix not in {".py", ".js", ".ts"} for name in upstream | changed):
            return None
        if not mixed and any(not name.endswith(".py") or "/" in name for name in upstream | changed):
            return None
        revisions = {}
        for revision in dict.fromkeys((merge_base, reviewed, current, final)):
            sources = {}
            listing = git(path, "ls-tree", "-r", "--full-tree", revision, runner=runner)
            if len(listing) > 1_000_000:
                return None
            total_bytes = 0
            for line in listing.splitlines():
                metadata, name = line.split("\t", 1)
                mode, kind, oid = metadata.split()
                if mode != "100644" or kind != "blob":
                    return None
                if documentation_path(name) and not mixed:
                    continue
                if not mixed and (not name.endswith(".py") or "/" in name):
                    return None
                if len(sources) >= 1000:
                    return None
                blob = runner(
                    ["git", "cat-file", "blob", oid],
                    cwd=path,
                    check=True,
                    capture_output=True,
                    text=False,
                    timeout=60,
                )
                if blob.returncode or not isinstance(blob.stdout, bytes) or len(blob.stdout) > 100_000:
                    return None
                total_bytes += len(blob.stdout)
                if total_bytes > 5_000_000:
                    return None
                sources[name] = blob.stdout.decode("utf-8")
            revisions[revision] = sources
        dependencies = (mixed_certificate if mixed else dependency_certificate)(revisions, upstream, changed)
        if not dependencies:
            return None
        tree = git(path, "merge-tree", "--write-tree", current, final, runner=runner)
    except ControlDeadlineExceeded:
        raise
    except (OSError, ValueError, SyntaxError, RecursionError, subprocess.SubprocessError):
        return None
    return {
        "kind": "closed_mixed_dependencies" if mixed else "closed_python_dependencies",
        "reviewed_base_sha": reviewed,
        "base_sha": current,
        "final_sha": final,
        "merge_base_sha": merge_base,
        "upstream_paths": sorted(upstream),
        "reviewed_paths": sorted(changed),
        "merge_tree_sha": tree,
        "dependency_certificate": dependencies,
    }

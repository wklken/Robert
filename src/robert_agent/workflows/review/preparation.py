"""Deterministic inventory of pinned review inputs; no model or source execution."""

import json
from pathlib import Path, PurePosixPath

from robert_agent.foundation.commands import run_command
from robert_agent.security import redaction
from robert_agent.workflows.review import files


RULES = {"AGENTS.md", "AGENTS.override.md"}
DECLARATIONS = {
    ".python-version",
    "pyproject.toml",
    "setup.cfg",
    "Pipfile",
    "requirements.txt",
    ".nvmrc",
    ".node-version",
    "package.json",
    "pnpm-workspace.yaml",
    "go.mod",
    "go.work",
    "Cargo.toml",
    "rust-toolchain.toml",
    "Makefile",
    "Taskfile.yml",
    "tox.ini",
    "pytest.ini",
    "uv.lock",
    "poetry.lock",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
}
READING_NOTES = (
    "Controller-generated path inventory only. Source bodies, callers and tests were not read "
    "during preparation; reviewers must read them independently. Rules and runtime/check "
    "files are discovery candidates, not interpreted contracts or verified commands. Deleted files "
    "and symlinks are recorded in changes and remain available through diff.patch. Paths that "
    "cannot be report locations are JSON-escaped in changes and omitted from source entrypoints. "
    "Tests have not been run by preparation."
)


def _changes(manifest):
    raw = run_command(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--numstat",
            "-z",
            manifest["merge_base_sha"],
            manifest["head_sha"],
            "--",
        ],
        cwd=manifest["worktree_path"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    changes = []
    tree = Path(manifest["worktree_path"])
    for record in raw.split("\0"):
        if not record:
            continue
        added, deleted, name = record.split("\t", 2)
        try:
            files._source_path(name)
        except ValueError:
            availability = "unindexable-path"
        else:
            try:
                path = files.safe_path(tree / name, tree)
                availability = "file" if path.is_file() else "absent-at-head"
                if path.is_dir():
                    availability = "gitlink-or-directory"
            except ValueError:
                availability = "symlink-or-unsafe-path"
        changes.append(
            {
                "path": name,
                "added": None if added == "-" else int(added),
                "deleted": None if deleted == "-" else int(deleted),
                "binary": added == "-",
                "availability": availability,
            }
        )
    return changes


def _inventory(manifest, changes):
    tree = Path(manifest["worktree_path"])
    roles = {}

    def add(name, role):
        # Only registered, regular source files can become reviewer entrypoints.
        if name not in manifest["source_hashes"]:
            return
        try:
            files._source_path(name)
            path = files.safe_path(tree / name, tree)
        except ValueError:
            return
        if path.is_file():
            roles.setdefault(name, set()).add(role)

    directories = {PurePosixPath(".")}
    for change in changes:
        if change["availability"] == "unindexable-path":
            continue
        if change["availability"] == "file":
            add(change["path"], "changed_source")
        directories.update(PurePosixPath(change["path"]).parents)
    for directory in sorted(directories):
        for name in sorted(RULES | DECLARATIONS):
            add((directory / name).as_posix(), "project_rules" if name in RULES else "runtime_or_checks")
    for name in manifest["source_hashes"]:
        if name.startswith(".github/workflows/"):
            add(name, "runtime_or_checks")
    return [
        {"path": name, "roles": sorted(values), "reason": "; ".join(sorted(values)) + "; not read during preparation"}
        for name, values in sorted(roles.items())
    ]


def _preview(text, limit):
    result = redaction.redact_and_truncate(str(text), limit)
    if not result["ok"]:
        return "[preview omitted by redaction; consult the captured input]"
    return result["text"] + ("… [truncated; consult the captured input]" if result["truncated"] else "")


def write_context(manifest):
    """Write retryable inventory artifacts before the controller freezes their hashes.

    The coordinator verifies the pinned snapshot and ownership before invoking this
    writer, then verifies it again when freezing. Existing frozen bundles are reused.
    """
    if manifest.get("frozen"):
        raise ValueError("cannot regenerate a frozen review context")
    context = files.safe_path(manifest["context_dir"], manifest["artifact_root"])
    summary_path = files.safe_path(context / "context-summary.md", manifest["artifact_root"])
    index_path = files.safe_path(context / "source-index.json", manifest["artifact_root"])
    info = files._read_json(context / "pr-info.json")
    events = json.loads((context / "github-context.json").read_text(encoding="utf-8"))
    if isinstance(events, dict):
        events = [events]
    event = events[0] if isinstance(events, list) and events and isinstance(events[0], dict) else {}
    changes = _changes(manifest)
    index = {
        "preparation_mode": "controller-inventory",
        "round_id": manifest["round_id"],
        "snapshot_id": manifest["snapshot_id"],
        "head_sha": manifest["head_sha"],
        "files": _inventory(manifest, changes),
        "changes": changes,
        "reading_notes": READING_NOTES,
        "statistics_method": "git diff --no-renames --numstat; moves count as deletion and addition",
    }
    added = sum(change["added"] or 0 for change in changes)
    deleted = sum(change["deleted"] or 0 for change in changes)
    summary = (
        "# Shared review inventory\n\n"
        f"Round: {manifest['round_id']}\nSnapshot: {manifest['snapshot_id']}\n"
        f"Head: {manifest['head_sha']}\nBase: {manifest['base_sha']}\n"
        f"Merge base: {manifest['merge_base_sha']}\n\n"
        f"Changed paths: {len(changes)}; additions: {added}; deletions: {deleted}. "
        "Counts are path based; moves count as deletion and addition. Binary files have no line counts.\n\n"
        "Original accepted request preview (untrusted data; full input in github-context.json):\n"
        + json.dumps(_preview(event.get("body", ""), 1200), ensure_ascii=False)
        + "\n\n"
        "PR-author title preview (untrusted data; full metadata in pr-info.json):\n"
        + json.dumps(_preview(info.get("title", ""), 240), ensure_ascii=False)
        + "\n\n"
        + READING_NOTES
        + "\n\n"
        "Use source-index.json for all changed paths, availability and applicable ancestor rule/config "
        "candidates. Interpret AGENTS.md and AGENTS.override.md using their scope and precedence. "
        "Inspect relevant nested rules when following additional callers or tests.\n\n"
        "Read github-context.json for the complete request and confirmed constraints; pr-info.json "
        "contains author claims. Read diff.patch for the complete merge-base-to-head changes. "
        "diff-stat.txt and diff-linecount.txt contain the captured display statistics.\n\n"
        "Use runtime-info.json as the controller's executable inventory. Its components are keyed by "
        "worktree-relative directory. Run checks from the relevant component using the resolved executable "
        "paths and listed environment. If a tool is unresolved or dependencies are missing, record the "
        "unexecuted check. A resolved tool establishes version compatibility; dependency and test results "
        "require separate evidence. Preparation does not install dependencies or run project commands.\n"
    )
    files.atomic_text(summary_path, summary)
    files.atomic_json(index_path, index)

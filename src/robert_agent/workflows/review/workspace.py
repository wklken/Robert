"""Immutable, round-specific Git source and context bundles for PR review."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from robert_agent.adapters.git.review_revision import worktree_state
from robert_agent.adapters.git.source_files import worktree_files
from robert_agent.foundation.commands import run_command
from robert_agent.context.native_prompts import review_evaluate_input
from robert_agent.context.prompts import render_prompt

from robert_agent.workflows.review import files as review_files
from robert_agent.workflows.review.project_runtime import runtime_info


CONTEXT_FILES = (
    "pr-info.json",
    "github-context.json",
    "changed-files.txt",
    "diff.patch",
    "diff-stat.txt",
    "diff-linecount.txt",
    "runtime-info.json",
)
PRIMARY_FILES = ("context-summary.md", "source-index.json")
ISOLATED_CHECKS_PROMPT_VERSION = 4
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _git(repo, *args):
    return run_command(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path):
    if path.is_symlink():
        return _digest("symlink:" + str(path.readlink()))
    return review_files.sha256_file(path)


def _source_hashes(tree, names=None):
    if names is None:
        names = _git(tree, "ls-files", "-z").split("\0")
    result = {}
    for name in names:
        if not name:
            continue
        path = tree / name
        # Submodules are represented by their Git commit, never followed.
        if path.is_dir() and not path.is_symlink():
            result[name] = "gitlink:" + _git(path, "rev-parse", "HEAD").strip()
        else:
            result[name] = _file_digest(path)
    return result


CACHE_ARTIFACTS = {
    ".pytest_cache": re.compile(
        r"\.gitignore|CACHEDIR\.TAG|README\.md|nodeids|v/cache/(?:nodeids|lastfailed|stepwise)"
    ),
    ".mypy_cache": re.compile(r"\.gitignore|CACHEDIR\.TAG|\d+\.\d+/(?:cache\.\d+\.db|[\w./-]+\.(?:data|meta)\.json)"),
    ".ruff_cache": re.compile(r"\.gitignore|CACHEDIR\.TAG|\d+\.\d+\.\d+/\d+"),
    ".import_linter_cache": re.compile(
        r"\.gitignore|CACHEDIR\.TAG|[\w.]+\.meta\.json|(?:[0-9a-f]{32}|[0-9a-f]{40})\.data\.json"
    ),
}


def _is_test_artifact(tree, name):
    # Git ignore rules are not authority to exclude arbitrary review inputs.
    # Only known generated files may be absent from the tracked source hashes.
    try:
        path = review_files.safe_path(tree / name, tree)
    except ValueError:
        return False
    if not path.is_file():
        return False
    relative = Path(name)
    if relative.name == "bkcore_unittest.sqlite3" or (
        relative.parent.name == "__pycache__" and relative.suffix == ".pyc"
    ):
        return True
    for parent in relative.parents:
        if parent.name in CACHE_ARTIFACTS:
            return CACHE_ARTIFACTS[parent.name].fullmatch(relative.relative_to(parent).as_posix()) is not None
    return False


def _make_source_read_only(tree):
    def fail(error):
        raise error

    for directory, _, files in os.walk(tree, topdown=False, onerror=fail):
        # A tracked symlink belongs to the snapshot, but its target may not.
        for path in (*(Path(directory, name) for name in files), Path(directory)):
            mode = path.lstat().st_mode
            if not stat.S_ISLNK(mode) and mode & _WRITE_BITS:
                path.chmod(stat.S_IMODE(mode) & ~_WRITE_BITS)


def _check_worktree_path(manifest, run_id):
    if not re.fullmatch(r"review-run-[0-9a-f]{32}", run_id):
        raise ValueError("invalid review run ID")
    root = Path(manifest["artifact_root"])
    return review_files.safe_path(root / "review-checkouts" / run_id, root, subdirectory="review-checkouts")


def create_check_worktree(manifest, run_id):
    """Create a private checkout of the pinned head with independent Git metadata."""
    tree = Path(manifest["worktree_path"])
    checkout = _check_worktree_path(manifest, run_id)
    checkout.parent.mkdir(exist_ok=True)
    checkout.mkdir(mode=0o700)
    # Borrow immutable objects; checkout bytes, Git index and refs are private.
    _git(tree, "clone", "--shared", "--no-checkout", "--", str(tree), str(checkout))
    _git(checkout, "remote", "remove", "origin")
    _git(checkout, "checkout", "--detach", manifest["head_sha"])
    return checkout


def discard_check_worktree(manifest, run_id):
    """Discard a registered run's disposable checkout after its process has exited."""
    try:
        checkout = _check_worktree_path(manifest, run_id)
    except ValueError:
        return
    shutil.rmtree(checkout, ignore_errors=True)


def _trigger(github_context):
    if isinstance(github_context, list):
        event = github_context[0] if github_context else {}
    else:
        event = github_context or {}
    if not isinstance(event, dict):
        return {}
    return {
        "trigger_event_fingerprint": event.get("event_fingerprint", ""),
        "trigger_url": event.get("url") or event.get("html_url") or event.get("source_url", ""),
    }


def prepare_snapshot(
    repo_root, worktree_root, artifact_root, round_id, pr_info, github_context, *, isolated_checks=False
):
    """Fetch a captured PR revision and create a new detached worktree.

    ``artifact_root`` is the configured review artifact base. Returned paths add
    round_id/snapshot_id. No existing worktree is reset, reused, or overwritten.
    GitHub metadata must be captured by the trusted caller before this function.
    """
    if not isinstance(round_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", round_id):
        raise ValueError("invalid review round_id")
    number = pr_info.get("number")
    if type(number) is not int or number < 1:
        raise ValueError("PR number must be a positive integer")
    head = pr_info.get("head") or {}
    base = pr_info.get("base") or {}
    head_sha, base_sha, base_ref = head.get("sha"), base.get("sha"), base.get("ref")
    for value in (head_sha, base_sha):
        if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", value):
            raise ValueError("PR head/base SHA must be complete Git object ids")
    if not isinstance(base_ref, str) or base_ref.startswith("-"):
        raise ValueError("invalid PR base branch")
    repo_root = Path(repo_root).resolve()
    worktree_root = Path(worktree_root).resolve()
    artifact_root = Path(artifact_root).resolve()
    _git(repo_root, "check-ref-format", "--branch", base_ref)
    head_ref = f"refs/robert/review-rounds/{round_id}/head"
    base_local_ref = f"refs/robert/review-rounds/{round_id}/base"
    _git(repo_root, "fetch", "upstream", f"+pull/{number}/head:{head_ref}")
    # The PR API's captured base commit can lag the target branch tip. Fetch
    # that exact revision so snapshot and publication checks use the same base.
    _git(repo_root, "fetch", "upstream", f"+{base_sha}:{base_local_ref}")
    if (
        _git(repo_root, "rev-parse", head_ref).strip() != head_sha
        or _git(repo_root, "rev-parse", base_local_ref).strip() != base_sha
    ):
        raise ValueError("PR snapshot changed while fetching; recapture PR metadata")
    return _build_snapshot(
        repo_root, worktree_root, artifact_root, round_id, pr_info, github_context, isolated_checks=isolated_checks
    )


def prepare_candidate_snapshot(
    repo_root,
    worktree_root,
    artifact_root,
    round_id,
    repo,
    base_ref,
    base_sha,
    head_sha,
    event,
    *,
    original_request=None,
    review_report_mode=None,
):
    info = {
        "head": {"sha": head_sha},
        "base": {"ref": base_ref, "sha": base_sha, "repo": {"full_name": repo}},
        "kind": "new-pr-candidate",
    }
    return _build_snapshot(
        Path(repo_root).resolve(),
        Path(worktree_root).resolve(),
        Path(artifact_root).resolve(),
        round_id,
        info,
        [event],
        original_request=original_request,
        review_report_mode=review_report_mode,
    )


def _build_snapshot(
    repo_root,
    worktree_root,
    artifact_root,
    round_id,
    pr_info,
    github_context,
    *,
    original_request=None,
    review_report_mode=None,
    isolated_checks=False,
):
    number = pr_info.get("number")
    head_sha = pr_info["head"]["sha"]
    base_sha, base_ref = pr_info["base"]["sha"], pr_info["base"]["ref"]
    merge_base = _git(repo_root, "merge-base", base_sha, head_sha).strip()
    identity = [round_id, number, head_sha, base_sha, base_ref, merge_base]
    if original_request:
        identity.append(original_request)
    snapshot_id = _digest(json.dumps(identity))[:24]
    tree = worktree_root / "review" / (f"pr-{number}" if number else "candidate") / round_id / snapshot_id
    artifacts = artifact_root / round_id / snapshot_id
    context = artifacts / "context"
    if tree.exists() or artifacts.exists():
        saved_path = review_files.safe_path(context / "manifest.json", artifact_root)
        saved = review_files._read_json(saved_path)
        expected = {
            "round_id": round_id,
            "snapshot_id": snapshot_id,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "base_ref": base_ref,
            "merge_base_sha": merge_base,
            "pr_number": number,
            "worktree_path": str(tree),
            "context_dir": str(context),
            "artifact_root": str(artifacts),
        }
        if (
            any(saved.get(key) != value for key, value in expected.items())
            or saved.get("review_report_mode") != review_report_mode
        ):
            raise ValueError("existing round snapshot identity does not match")
        if _trigger(github_context).get("trigger_event_fingerprint") != saved.get("trigger_event_fingerprint"):
            raise ValueError("existing round trigger identity does not match")
        verify_snapshot(saved, require_current_prompt=True)
        return saved
    tree.parent.mkdir(parents=True, exist_ok=True)
    context.mkdir(parents=True)
    _git(repo_root, "worktree", "add", "--detach", str(tree), head_sha)
    if isolated_checks:
        _make_source_read_only(tree)
    diff = _git(
        tree,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        merge_base,
        head_sha,
        "--",
    )
    changed_paths = _git(tree, "diff", "--no-ext-diff", "--name-only", merge_base, head_sha, "--")
    content = {
        "pr-info.json": json.dumps(pr_info, ensure_ascii=False, indent=2) + "\n",
        "github-context.json": json.dumps(github_context, ensure_ascii=False, indent=2) + "\n",
        "changed-files.txt": changed_paths,
        "diff.patch": diff,
        "diff-stat.txt": _git(tree, "diff", "--no-ext-diff", "--stat", merge_base, head_sha, "--"),
        "diff-linecount.txt": str(len(diff.splitlines())) + "\n",
        "runtime-info.json": json.dumps(runtime_info(tree, changed_paths.splitlines()), ensure_ascii=False, indent=2)
        + "\n",
    }
    for name, text in content.items():
        (context / name).write_text(text, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "context_version": 1,
        "prompt_version": ISOLATED_CHECKS_PROMPT_VERSION if isolated_checks else 3,
        "round_id": round_id,
        "snapshot_id": snapshot_id,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "base_ref": base_ref,
        "merge_base_sha": merge_base,
        "pr_number": number,
        "repo": (pr_info["base"].get("repo") or {}).get("full_name", ""),
        "worktree_path": str(tree),
        "context_dir": str(context),
        "artifact_root": str(artifacts),
        "source_hashes": _source_hashes(tree),
        "files": {str(context / name): review_files.sha256_file(context / name) for name in CONTEXT_FILES},
        "frozen": False,
        **({"original_request": original_request} if original_request else {}),
        **({"review_report_mode": review_report_mode} if review_report_mode else {}),
        **_trigger(github_context),
    }
    review_files.atomic_json(context / "manifest.json", manifest)
    return manifest


def _reviewer_text(manifest, *, stage_version=None):
    return render_prompt(
        review_evaluate_input(
            manifest, severities=review_files.SEVERITIES, recommendations=review_files.RECOMMENDATIONS
        ),
        stage_version=stage_version,
    ).text


def verify_snapshot(manifest, *, require_current_prompt=False):
    """Verify frozen bytes against the manifest, or check an unregistered snapshot before adoption."""
    tree = Path(manifest["worktree_path"])
    try:
        head, dirty = worktree_state(tree, read_git=_git)
        if head != manifest["head_sha"]:
            raise ValueError("review worktree HEAD changed")
        if dirty:
            raise ValueError("review worktree source is dirty")
        tracked, untracked = worktree_files(tree, read_git=_git)
        version = manifest.get("prompt_version")
        isolated_checks = version == ISOLATED_CHECKS_PROMPT_VERSION
        if untracked and (isolated_checks or any(not _is_test_artifact(tree, name) for name in untracked)):
            raise ValueError("review worktree contains unregistered files")
        if _source_hashes(tree, tracked) != manifest["source_hashes"]:
            raise ValueError("review worktree source hash changed")
        for name, expected in manifest["files"].items():
            path = review_files.safe_path(name, manifest["artifact_root"])
            if review_files.sha256_file(path) != expected:
                raise ValueError("review context file hash changed")
        if manifest.get("frozen"):
            path = review_files.safe_path(manifest["prompt_path"], manifest["artifact_root"])
            if review_files.sha256_file(path) != manifest["prompt_sha256"]:
                raise ValueError("review prompt hash changed")
            if require_current_prompt:
                # Version 1 was also used by the first controller-inventory release.
                # Adoption must match a known full rendering, not only a local hash.
                if type(version) is not int or version not in (1, 2, 3, ISOLATED_CHECKS_PROMPT_VERSION):
                    raise ValueError("unsupported review prompt version")
                text = path.read_text(encoding="utf-8")
                if text != _reviewer_text(manifest, stage_version=2 if version == 1 else version) and not (
                    version == 1 and text == _reviewer_text(manifest, stage_version=1)
                ):
                    raise ValueError("review prompt no longer matches a supported built-in template")
            saved = json.loads(Path(manifest["context_dir"], "manifest.json").read_text(encoding="utf-8"))
            if saved != manifest:
                raise ValueError("review manifest changed")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError("review snapshot is missing or invalid") from exc
    return True


def freeze_context(manifest):
    """Validate primary-prepared files and freeze one shared reviewer prompt."""
    verify_snapshot(manifest)
    if manifest.get("frozen"):
        return manifest
    saved_path = review_files.safe_path(Path(manifest["context_dir"]) / "manifest.json", manifest["artifact_root"])
    saved = review_files._read_json(saved_path)
    if saved.get("frozen"):
        # The manifest may have committed before the controller registered it.
        excluded = {"files", "frozen", "prompt_path", "prompt_sha256"}
        if any(saved.get(key) != value for key, value in manifest.items() if key not in excluded):
            raise ValueError("frozen manifest identity differs from prepared context")
        if any(saved.get("files", {}).get(key) != value for key, value in manifest["files"].items()):
            raise ValueError("frozen manifest changed prepared file hashes")
        expected_files = set(manifest["files"]) | {str(Path(manifest["context_dir"]) / name) for name in PRIMARY_FILES}
        if set(saved.get("files", {})) != expected_files:
            raise ValueError("frozen manifest has unexpected context files")
        verify_snapshot(saved, require_current_prompt=True)
        return saved
    context = Path(manifest["context_dir"])
    summary = review_files.safe_path(context / "context-summary.md", manifest["artifact_root"])
    index_path = review_files.safe_path(context / "source-index.json", manifest["artifact_root"])
    try:
        if not summary.read_text(encoding="utf-8").strip():
            raise ValueError("primary context summary is empty")
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("primary context files are missing or invalid") from exc
    if not isinstance(index, dict) or not isinstance(index.get("files"), list):
        raise ValueError("source-index requires files array")
    if not isinstance(index.get("reading_notes"), str) or not index["reading_notes"].strip():
        raise ValueError("source-index requires actual reading_notes")
    tree = Path(manifest["worktree_path"])
    for entry in index["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError("source-index entries require path and reason")
        name = review_files._source_path(entry.get("path"))
        path = review_files.safe_path(tree / name, tree)
        if not path.is_file():
            raise ValueError("source-index references a missing source file")
    frozen = {**manifest, "files": dict(manifest["files"]), "frozen": True}
    for name in PRIMARY_FILES:
        frozen["files"][str(context / name)] = review_files.sha256_file(context / name)
    prompt_path = Path(manifest["artifact_root"]) / "reviewer-prompt.md"
    review_files.safe_path(prompt_path, manifest["artifact_root"])
    # Some version 1 snapshots already used the controller-inventory template.
    version = frozen["prompt_version"]
    expected_prompt = _reviewer_text(frozen, stage_version=2 if version == 1 else version)
    if prompt_path.exists():
        if prompt_path.read_text(encoding="utf-8") != expected_prompt:
            raise ValueError("unregistered reviewer prompt differs from frozen context")
    else:
        prompt_path.write_text(expected_prompt, encoding="utf-8")
    frozen.update(
        prompt_path=str(prompt_path),
        prompt_sha256=review_files.sha256_file(prompt_path),
    )
    review_files.atomic_json(context / "manifest.json", frozen)
    return frozen

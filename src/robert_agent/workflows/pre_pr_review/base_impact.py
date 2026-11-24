"""Validate and persist compatibility without rewriting a frozen snapshot."""

import json
from pathlib import Path
import subprocess

from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.adapters.git.merge_preflight import MergePreflightError, check_merge_environment
from robert_agent.adapters.git.workspace import check_worktree_reuse
from robert_agent.delivery.base_revisions import compatibility_refresh
from robert_agent.execution import retry
from robert_agent.execution.stages import StageStopped, attempt_is_active
from robert_agent.persistence import transactions
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.review import workspace


def reconcile_operator_merge(driver, candidate, current_base):
    """Reconcile an explicit retry's earlier merge before handling newer upstream."""
    reviewed_base = driver.manifest.get("base_sha")
    if not driver.request.get("retried_from_task") or not reviewed_base or reviewed_base == current_base:
        return candidate
    preserved = driver.preserve_completed_repair(candidate, align=False)
    error = check_worktree_reuse(
        driver.repo["repo_root"], driver.repo["worktree_root"], driver.assigned_path, branch_name=driver.branch
    )
    if error:
        raise StageStopped(error)
    try:
        current = clean_head(driver.assigned_path, driver.branch)
    except ValueError as exc:
        raise StageStopped(str(exc)) from exc
    if current in {driver.manifest["head_sha"], preserved["head"]}:
        return candidate
    parents = git(driver.assigned_path, "show", "-s", "--format=%P", current).split()
    if len(parents) != 2 or parents[0] not in {candidate["head"], preserved["head"]}:
        raise StageStopped("assigned branch is not the exact intended base merge")
    merged_base = parents[1]
    git(driver.repo["repo_root"], "fetch", "upstream", current_base)
    if (
        git(driver.assigned_path, "merge-base", reviewed_base, merged_base) != reviewed_base
        or git(driver.assigned_path, "merge-base", merged_base, current_base) != merged_base
    ):
        raise StageStopped("completed merge base is outside the current upstream history")
    # The existing owner/checkpoint/tree checks authenticate the original
    # operation; a newer base then receives its own snapshot and merge.
    if parents[0] != preserved["head"]:
        candidate = reconcile_repair_merge(driver, candidate, preserved, merged_base, current)
    else:
        candidate = driver.merge_current_base(preserved, merged_base)
    driver.save_candidate_snapshot(candidate, merged_base)
    return candidate


def reconcile_repair_merge(driver, candidate, preserved, base_sha, merged):
    """Checkpoint the exact old base merge and its authenticated repair together."""
    driver.check_active()
    error = check_worktree_reuse(
        driver.repo["repo_root"], driver.repo["worktree_root"], driver.assigned_path, branch_name=driver.branch
    )
    if error:
        raise StageStopped(error)
    current = clean_head(driver.assigned_path, driver.branch)
    git(driver.repo["repo_root"], "fetch", "upstream", base_sha)
    if git(driver.assigned_path, "merge-base", driver.manifest["base_sha"], base_sha) != driver.manifest["base_sha"]:
        raise StageStopped("completed merge base is outside the reviewed upstream history")
    original_tree = git(driver.assigned_path, "merge-tree", "--write-tree", candidate["head"], base_sha)
    driver.verify_base_merge(
        merged, {"candidate_sha": candidate["head"], "base_sha": base_sha, "tree_sha": original_tree}
    )
    repair_sha = preserved["head"]
    if git(driver.assigned_path, "merge-base", merged, repair_sha) == repair_sha:
        return driver.merge_current_base(candidate, base_sha)
    try:
        tree = git(driver.assigned_path, "merge-tree", "--write-tree", merged, repair_sha)
    except subprocess.CalledProcessError as exc:
        raise StageStopped(
            "completed base merge conflicts with the registered repair; conflict resolution is required"
        ) from exc
    checkpoint = {
        "version": 1,
        "round_id": driver.round_id,
        "snapshot_id": driver.manifest["snapshot_id"],
        "previous_base_sha": driver.manifest["base_sha"],
        "candidate_sha": candidate["head"],
        "base_sha": base_sha,
        "tree_sha": tree,
        "branch": driver.branch,
        "worktree_path": str(Path(driver.assigned_path).resolve()),
        "repair_sha": repair_sha,
        "repair_parent_sha": merged,
    }
    if current != merged:
        driver.verify_base_merge(current, {"candidate_sha": merged, "base_sha": repair_sha, "tree_sha": tree})
    else:
        try:
            check_merge_environment(driver.assigned_path)
        except MergePreflightError as exc:
            raise StageStopped(str(exc)) from exc
    with store.connect(driver.db_path) as conn:
        transactions.begin(conn, immediate=True)
        if not attempt_is_active(conn, driver.task_id, driver.attempt_id):
            raise StageStopped("base merge owner changed")
        entry = store.session(conn, task_id=driver.task_id)
        if entry["snapshot_id"] != checkpoint["snapshot_id"]:
            raise StageStopped("base merge snapshot changed")
        request = dict(entry["request"])
        saved = request.get("base_merge")
        if saved:
            if saved != checkpoint:
                raise StageStopped("base merge checkpoint changed")
        else:
            if not request.get("retried_from_task"):
                raise StageStopped("completed base merge requires an explicit retrigger")
            count = request.get("base_refresh_count", 0)
            if count >= retry.MAX_ATTEMPTS:
                raise StageStopped("base refresh budget exhausted; keep checkpoint for a later retry")
            request.update(base_merge=checkpoint, base_refresh_count=count + 1)
            request.pop("final_feedback", None)
            store.update(conn, driver.round_id, request_json=json.dumps(request, sort_keys=True), proof_json=None)
    driver.request.update(request)
    driver.request.pop("final_feedback", None)
    driver.final_feedback = None
    driver.check_active()
    if clean_head(driver.assigned_path, driver.branch) != current:
        raise StageStopped("candidate branch changed after base merge checkpoint")
    if current == merged:
        git(driver.assigned_path, "merge", "--no-ff", "--no-edit", repair_sha)
    final = clean_head(driver.assigned_path, driver.branch)
    driver.verify_base_merge(final, {"candidate_sha": merged, "base_sha": repair_sha, "tree_sha": tree})
    return {**candidate, "head": final}


def verify_original(driver):
    """Verify original source, context and handoff before observing/merging a base."""
    if not driver.manifest.get("snapshot_id"):
        return
    workspace.verify_snapshot(driver.manifest)
    git(driver.repo["repo_root"], "rev-parse", driver.manifest["base_sha"] + "^{commit}")
    candidate = driver.manifest["candidate"]
    if store.digest(candidate["candidate_path"]) != candidate["candidate_sha256"]:
        raise StageStopped("candidate handoff changed before base impact check")
    original = driver.manifest.get("original_request")
    if original and store.digest(original["path"]) != original["sha256"]:
        raise StageStopped("pinned original request changed")
    with store.connect(driver.db_path) as conn:
        entry = store.session(conn, task_id=driver.task_id)
    verify_saved(driver, entry["request"].get("base_compatibility"))


def verify_saved(driver, saved):
    if saved is None:
        return
    expected = {
        "version": 1,
        "round_id": driver.round_id,
        "snapshot_id": driver.manifest["snapshot_id"],
        "candidate_sha": driver.manifest["head_sha"],
        "reviewed_base_sha": driver.manifest["base_sha"],
    }
    if (
        not isinstance(saved, dict)
        or type(saved.get("version")) is not int
        or any(saved.get(key) != value for key, value in expected.items())
    ):
        raise StageStopped("base compatibility scope changed")
    if not saved.get("base_sha") or not saved.get("final_sha"):
        raise StageStopped("base compatibility certificate is incomplete")
    actual = compatibility_refresh(
        driver.repo["repo_root"],
        saved["reviewed_base_sha"],
        saved["base_sha"],
        saved["final_sha"],
        round_id=driver.round_id,
        snapshot_id=saved["snapshot_id"],
        candidate=saved["candidate_sha"],
    )
    if actual != saved:
        raise StageStopped("base compatibility certificate changed")


def save_compatibility(driver, certificate):
    driver.check_active()
    with store.connect(driver.db_path) as conn:
        entry = store.session(conn, task_id=driver.task_id)
    previous = entry["request"].get("base_compatibility")
    verify_saved(driver, previous)
    error = check_worktree_reuse(
        driver.repo["repo_root"], driver.repo["worktree_root"], driver.assigned_path, branch_name=driver.branch
    )
    if error:
        raise StageStopped(error)
    current = clean_head(driver.assigned_path, driver.branch)
    if current not in {driver.manifest["candidate"]["head"], certificate["final_sha"]}:
        # Exact merge recovery belongs to the existing affected-base checkpoint.
        return False
    with store.connect(driver.db_path) as conn:
        transactions.begin(conn, immediate=True)
        if not attempt_is_active(conn, driver.task_id, driver.attempt_id):
            raise StageStopped("base compatibility owner changed")
        entry = store.session(conn, task_id=driver.task_id)
        if entry["snapshot_id"] != certificate["snapshot_id"]:
            raise StageStopped("base compatibility snapshot changed")
        request = dict(entry["request"])
        if request.get("base_compatibility") != previous:
            raise StageStopped("base compatibility changed during observation")
        request["base_compatibility"] = certificate
        store.update(conn, driver.round_id, request_json=json.dumps(request, sort_keys=True))
    driver.request["base_compatibility"] = certificate
    driver.base_refresh = certificate
    return True

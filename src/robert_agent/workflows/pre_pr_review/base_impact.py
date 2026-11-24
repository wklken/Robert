"""Validate and persist compatibility without rewriting a frozen snapshot."""

import json

from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.adapters.git.workspace import check_worktree_reuse
from robert_agent.delivery.base_revisions import compatibility_refresh
from robert_agent.execution.stages import StageStopped, attempt_is_active
from robert_agent.persistence import transactions
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.workflows.review import workspace


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

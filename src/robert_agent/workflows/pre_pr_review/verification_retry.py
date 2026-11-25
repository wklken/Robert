"""Fresh verification after a rejected handoff, retaining its original evidence."""

import hashlib
import json
import os
from pathlib import Path

from robert_agent.adapters.git.review_revision import clean_head
from robert_agent.execution import stage_evidence
from robert_agent.execution.stages import StageStopped
from robert_agent.persistence.rounds import pre_pr_review as store
from robert_agent.persistence.rounds.records import artifact_available


GATE_ERRORS = {
    "final verification requires every required check to pass",
    "final verification contains an unsuccessful check",
    "Public text requires redaction before submission.",
}


def _needs_fresh_checks(run):
    return (run["status"] == "failed" and run["error"] in GATE_ERRORS and not artifact_available(run)) or (
        artifact_available(run)
        and run["artifact"].get("operator_question")
        and (run["dispatch"].get("result_checkpoint") or {}).get("source_pin_error")
    )


def inputs(driver):
    with store.connect(driver.db_path) as conn:
        runs = store.runs(conn, driver.round_id)
    latest = next(
        (
            run
            for run in reversed(runs)
            if run["stage"] == "address_findings"
            and Path(run["output_dir"]) == Path(driver.manifest["artifact_root"]) / "address_findings" / run["run_id"]
            and _needs_fresh_checks(run)
        ),
        None,
    )
    if not latest:
        return None
    failed_gate = latest["status"] == "failed" and latest["error"] in GATE_ERRORS
    recorded = latest["dispatch"]
    pid = recorded.get("pid")
    if type(pid) is not int or pid <= 1 or recorded.get("process_group_id", pid) != pid:
        raise StageStopped("verification retry requires the original process identity")
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise StageStopped("verification retry process group cannot be verified") from exc
    else:
        raise StageStopped("verification retry requires the original process group to exit")
    source = driver.sealed_output(latest) / "dispositions.json"
    value = json.loads(source.read_text())
    if (
        value.get("candidate_sha") != driver.manifest["head_sha"]
        or value.get("report_sha256") != driver.verification_report_sha
    ):
        raise StageStopped("verification retry handoff identity changed")
    tree = driver.address_workspace(latest["run_id"])
    final = clean_head(tree)
    if final != value.get("final_sha"):
        raise StageStopped("verification retry source HEAD changed")
    checkpoint = recorded.get("result_checkpoint")
    current = driver.source_checkpoint(latest)
    if (
        not checkpoint
        or current.get("source_pin_error")
        or any(current.get(key) != item for key, item in checkpoint.items() if key != "source_pin_error")
    ):
        raise StageStopped("verification retry source checkpoint changed")
    # An incomplete old pin cannot authorize native resume/artifact adoption.
    # A fresh writer receives a new complete launch pin and fresh final checks.
    result = {
        "source_run_id": latest["run_id"],
        "handoff_path": str(source),
        "handoff_sha256": store.digest(source),
        "candidate_sha": value["candidate_sha"],
        "report_sha256": value["report_sha256"],
        "final_sha": final,
        "worktree_path": str(tree),
        "source_checkpoint": current,
        "error": latest["error"] if failed_gate else "question source checkpoint incomplete",
    }
    result["input_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def verify(inputs):
    if not inputs:
        return
    checkpoint = inputs["source_checkpoint"]
    manifest = {key: checkpoint.get(key) for key in ("snapshot_id", "head_sha", "base_sha")}
    if (
        store.digest(inputs["handoff_path"]) != inputs["handoff_sha256"]
        or clean_head(inputs["worktree_path"]) != inputs["final_sha"]
        or stage_evidence.observe_source(checkpoint, manifest, inputs["worktree_path"]) != checkpoint
    ):
        raise StageStopped("verification retry evidence changed")

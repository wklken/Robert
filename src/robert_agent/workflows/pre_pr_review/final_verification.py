"""A pinned, durable reviewer pass over repaired candidate changes."""

import hashlib
import json

from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.context.native_prompts import final_review_input
from robert_agent.context.prompts import render_prompt
from robert_agent.execution.stages import StageStopped, StageSuperseded
from robert_agent.persistence.rounds import pre_pr_review as store


def verify(coordinator, report, report_sha, finding_ids, disposition):
    head, final = coordinator.manifest["head_sha"], disposition["head"]
    if head == final:
        return None
    coordinator.stage = "verify_final"
    coordinator.phase(coordinator.stage)
    # Addressing has already waited for the writer and its process group to
    # exit. Reuse that round-owned checkout instead of creating another one.
    tree = coordinator.address_workspace(disposition["run_id"])
    if clean_head(tree) != final:
        raise StageSuperseded("final reviewer checkout changed")
    diff = coordinator.prompt(f"additional-diff-{head}-{final}.patch", git(tree, "diff", head, final, "--"))
    identity = {
        "round_id": coordinator.round_id,
        "snapshot_id": coordinator.manifest["snapshot_id"],
        "candidate_sha": head,
        "final_sha": final,
        "report_sha256": report_sha,
        "dispositions_sha256": disposition["dispositions_sha256"],
        "additional_diff_sha256": store.digest(diff),
    }
    inputs = {
        **identity,
        "finding_ids": finding_ids,
        "report_path": str(report),
        "dispositions_path": disposition["dispositions_path"],
        "additional_diff_path": str(diff),
        "worktree_path": str(tree),
    }
    input_text = json.dumps(inputs, sort_keys=True, indent=2)
    key = hashlib.sha256(input_text.encode()).hexdigest()
    input_path = coordinator.prompt(f"final-review-inputs-{key}.json", input_text)
    coordinator.final_review = {**inputs, "input_path": str(input_path)}
    prompt = coordinator.prompt(f"verify-final-{key}.md", render_prompt(final_review_input(inputs, input_path)).text)

    def validate(output, _model):
        if clean_head(tree) != final:
            raise StageSuperseded("final reviewer checkout changed")
        for path, digest in (
            (report, report_sha),
            (disposition["dispositions_path"], disposition["dispositions_sha256"]),
            (diff, identity["additional_diff_sha256"]),
            (input_path, key),
        ):
            if store.digest(path) != digest:
                raise StageStopped("final reviewer input changed")
        path = output / "final-verification.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValueError("invalid final review schema")
        if any(value.get(name) != expected for name, expected in identity.items()):
            raise StageStopped("final reviewer identity does not match the repaired candidate")
        checked = value.get("checked_findings")
        if not isinstance(checked, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
            for item in checked
        ):
            raise ValueError("final review requires reasoned finding evidence")
        if len(checked) != len(finding_ids) or {item["id"] for item in checked} != set(finding_ids):
            raise StageStopped("final review omitted or duplicated prior findings")
        if (
            value.get("recommendation") != "approved"
            or value.get("blocking_findings") != []
            or any(item.get("status") not in {"resolved", "dismissed"} for item in checked)
        ):
            raise StageStopped("final review has blocking or unknown findings")
        return {**identity, "final_verification_path": str(path), "final_verification_sha256": store.digest(path)}

    return coordinator.run_phase("verify_final", coordinator.request["workers"][0], prompt, validate, input_hash=key)

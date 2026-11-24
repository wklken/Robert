"""A pinned, durable reviewer pass over repaired candidate changes."""

import hashlib
import json
from pathlib import Path

from robert_agent.adapters.git.review_revision import clean_head, git
from robert_agent.context.native_prompts import final_review_input
from robert_agent.context.prompts import render_prompt
from robert_agent.execution.stages import StageStopped, StageSuperseded
from robert_agent.persistence.rounds import pre_pr_review as store


class FinalReviewBlocked(StageStopped):
    def __init__(self, artifact, value, disposition):
        super().__init__("final review has blocking or unknown findings")
        self.artifact, self.value, self.disposition = artifact, value, disposition


def _registered_inputs(coordinator, inputs):
    """Reuse an exact registered preupgrade document despite report relocation."""
    root = coordinator.manifest.get("artifact_root")
    if not root:
        return None
    with store.connect(coordinator.db_path) as conn:
        previous = store.runs(conn, coordinator.round_id)
    semantic = {name: value for name, value in inputs.items() if name != "report_path"}
    for run in reversed(previous):
        if run["stage"] != "verify_final" or run["worker_id"] != coordinator.request["workers"][0]["name"]:
            continue
        prompt = Path(run["dispatch"].get("prompt_path", ""))
        key = prompt.stem.removeprefix("verify-final-")
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            continue
        if prompt != Path(root) / f"verify-final-{key}.md":
            raise StageStopped("registered final reviewer prompt path changed")
        input_path = Path(root) / f"final-review-inputs-{key}.json"
        try:
            document_bytes = input_path.read_bytes()
            document = json.loads(document_bytes)
            if not isinstance(document, dict):
                raise ValueError("invalid registered final reviewer inputs")
            if {name: value for name, value in document.items() if name != "report_path"} != semantic:
                continue
            full_sha = hashlib.sha256(document_bytes).hexdigest()
            # Historical keys hash the whole document. New keys exclude only
            # report location, and still require exact canonical JSON bytes.
            semantic_sha = hashlib.sha256(json.dumps(semantic, sort_keys=True, indent=2).encode()).hexdigest()
            if key == semantic_sha:
                if (
                    document["report_path"] != inputs["report_path"]
                    or document_bytes != json.dumps(document, sort_keys=True, indent=2).encode()
                ):
                    raise ValueError("registered final reviewer input bytes changed")
            elif key != full_sha:
                raise ValueError("registered final reviewer input digest changed")
            if store.digest(prompt) != run["prompt_sha256"]:
                raise ValueError("registered final reviewer prompt changed")
            if Path(document["report_path"]).read_bytes() != Path(inputs["report_path"]).read_bytes():
                raise ValueError("registered final reviewer report changed")
        except (KeyError, OSError, ValueError) as exc:
            raise StageStopped(str(exc)) from exc
        return document, input_path, key, full_sha
    return None


def verify(coordinator, report, report_sha, finding_ids, disposition):
    head, final = coordinator.manifest["head_sha"], disposition["head"]
    if head == final and not finding_ids:
        return None
    coordinator.stage = "verify_final"
    coordinator.phase(coordinator.stage)
    # Addressing has already waited for the writer and its process group to
    # exit. Reuse that round-owned checkout instead of creating another one.
    tree = coordinator.address_workspace(disposition["run_id"])
    if clean_head(tree) != final:
        raise StageSuperseded("final reviewer checkout changed")
    # Renderer versions may relocate an otherwise identical public report.
    # Freeze its exact bytes at a round-owned content address so both the
    # input document and any recorded verifier prompt remain reusable.
    report_bytes = Path(report).read_bytes()
    if hashlib.sha256(report_bytes).hexdigest() != report_sha:
        raise StageStopped("final reviewer input changed")
    pinned_report = coordinator.prompt(f"final-review-report-{report_sha}.md", report_bytes.decode("utf-8"))
    if store.digest(pinned_report) != report_sha:
        raise StageStopped("final reviewer input changed")
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
        "report_path": str(pinned_report),
        "dispositions_path": disposition["dispositions_path"],
        "additional_diff_path": str(diff),
        "worktree_path": str(tree),
        "original_request": coordinator.original_request,
        "decision_owner": coordinator.scope.get("decision_owner"),
        "operator_reply": coordinator.request.get("operator_reply"),
        "dispositions": json.loads(Path(disposition["dispositions_path"]).read_text(encoding="utf-8"))["dispositions"],
    }
    input_text = json.dumps(inputs, sort_keys=True, indent=2)
    # The run key expresses report content; the full input digest separately
    # protects the physical paths and every byte supplied to the reviewer.
    semantic_text = json.dumps(
        {name: value for name, value in inputs.items() if name != "report_path"}, sort_keys=True, indent=2
    )
    key = hashlib.sha256(semantic_text.encode()).hexdigest()
    input_sha = hashlib.sha256(input_text.encode()).hexdigest()
    registered = _registered_inputs(coordinator, inputs)
    if registered:
        inputs, input_path, key, input_sha = registered
        pinned_report = Path(inputs["report_path"])
    else:
        input_path = coordinator.prompt(f"final-review-inputs-{key}.json", input_text)
    coordinator.final_review = {**inputs, "input_path": str(input_path)}
    prompt = coordinator.prompt(
        f"verify-final-{key}.md",
        render_prompt(final_review_input(inputs, input_path)).text,
        stage="verify_final",
    )

    def validate(output, _model):
        if clean_head(tree) != final:
            raise StageSuperseded("final reviewer checkout changed")
        for path, digest in (
            (report, report_sha),
            (pinned_report, report_sha),
            (disposition["dispositions_path"], disposition["dispositions_sha256"]),
            (diff, identity["additional_diff_sha256"]),
            (input_path, input_sha),
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
        if value.get("recommendation") not in {"approved", "blocked", "unknown"}:
            raise ValueError("unsupported final review recommendation")
        if not isinstance(value.get("blocking_findings"), list) or any(
            not isinstance(item, str) or not item.strip() for item in value["blocking_findings"]
        ):
            raise ValueError("final review requires concrete blocker evidence")
        if any(
            item.get("status") not in {"resolved", "dismissed", "deferred", "blocking", "unknown"} for item in checked
        ):
            raise ValueError("unsupported checked finding status")
        dispositions = {item["id"]: item["status"] for item in inputs["dispositions"]}
        if any(item["status"] == "deferred" and dispositions[item["id"]] != "deferred" for item in checked):
            raise ValueError("final review cannot defer a finding without a matching disposition")
        return {**identity, "final_verification_path": str(path), "final_verification_sha256": store.digest(path)}

    artifact = coordinator.run_phase(
        "verify_final", coordinator.request["workers"][0], prompt, validate, input_hash=key
    )
    value = json.loads(Path(artifact["final_verification_path"]).read_text(encoding="utf-8"))
    if (
        value["recommendation"] != "approved"
        or value["blocking_findings"]
        or any(item["status"] in {"blocking", "unknown"} for item in value["checked_findings"])
    ):
        raise FinalReviewBlocked(artifact, value, disposition)
    return artifact

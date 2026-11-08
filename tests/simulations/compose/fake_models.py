"""File-protocol models for the real review and vote coordinators."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from git_transport import activity


_OUTPUTS = {
    "review.prepare": ("context-summary.md", "source-index.json"),
    "review.evaluate": ("result.json",),
    "review.synthesize": ("evaluation.json",),
    "vote.prepare": ("prepared-request.json",),
    "vote.cast": ("ballot.json",),
    "vote.synthesize": ("synthesis.json",),
}


def model_input(prompt):
    pieces = re.split(r"(?m)^## ([^\n]+)\n\n", prompt)
    sections = dict(zip(pieces[1::2], pieces[2::2]))
    if pieces[0] != "# Robert agent prompt\n\n" or len(sections) != len(pieces[1::2]):
        raise ValueError("invalid native prompt header")
    facts = dict(line.split(": ", 1) for line in sections["Stage"].splitlines() if line)
    stage = facts["Stage"]
    workflow = stage.split(".", 1)[0]
    env = f"ROBERT_{workflow.upper()}_OUTPUT_DIR"
    delivery = sections["Delivery"]
    outputs = tuple(re.findall(rf"(?m)^- {env}/([^\n]+)$", delivery))
    if outputs != _OUTPUTS[stage] or not delivery.startswith("Transport: atomic-files\n"):
        raise ValueError("native output contract changed")
    manifest_path = context_file(sections["Context"], "manifest.json")
    manifest = json.loads(manifest_path.read_text())
    for key in ("round_id", "snapshot_id"):
        if facts[key] != manifest[key]:
            raise ValueError(f"native prompt and manifest disagree: {key}")
    if stage.startswith("review.") and facts["head_sha"] != manifest["head_sha"]:
        raise ValueError("review head changed")
    return stage, env, manifest, sections["Context"]


def context_file(context, item):
    match = re.search(
        rf"(?m)^- {re.escape(item)} \(required, file, complete\):[^\n]*\n(?:  [^\n]*\n)*?  Read (.+) before acting\.$",
        context,
    )
    if not match:
        raise ValueError(f"missing file context: {item}")
    return Path(match[1])


def outputs(stage, manifest, context, worker, policy):
    value = {key: manifest[key] for key in ("schema_version", "round_id", "snapshot_id")}
    if stage.startswith("review."):
        value["head_sha"] = manifest["head_sha"]
    if stage == "review.prepare":
        path = Path(manifest["worktree_path"]) / "calc.py"
        path.read_text()
        return {
            "context-summary.md": "Read calc.py and the arithmetic change.\n",
            "source-index.json": {
                "files": [{"path": "calc.py", "reason": "Changed arithmetic"}],
                "reading_notes": "Read the entire changed file.",
            },
        }
    if stage == "review.evaluate":
        value.update(
            findings=[],
            dismissed_findings=[],
            coverage={"reviewed_paths": ["calc.py"], "unreviewed_paths": [], "notes": "Read the changed file."},
            merge_recommendation="ready to merge",
        )
    elif stage == "review.synthesize":
        evidence = json.loads(context_file(context, "selected-reviewer-results").read_text())
        if len(evidence["records"]) < 2:
            raise ValueError("fixture review synthesis is missing reviewers")
        value.update(items=[], merge_recommendation="ready to merge", merge_reason="没有已确认问题")
    elif stage == "vote.prepare":
        event = json.loads(context_file(context, "request.json").read_text())
        if policy.get("clarify_vote") and not event.get("clarifications"):
            value.update(status="needs_clarification", question="What is the latency target?")
        else:
            value.update(
                status="ready",
                question="Which event delivery mechanism should the fixture use?",
                options=[
                    {"id": "A", "description": "Polling", "origin": "user"},
                    {"id": "B", "description": "Webhook", "origin": "user"},
                ],
                criteria=["Reliability"],
                constraints=[],
                unknowns=[],
            )
    elif stage == "vote.cast":
        choice = policy.get("ballots", {}).get(worker, "A")
        value.update(
            ballot_spec_sha256=manifest["ballot_spec_sha256"],
            choice_id=choice,
            reason_summary=f"Reason from {worker}",
            evidence_refs=["source-1"],
            tradeoffs=[],
            uncertainties=[],
        )
        if choice is None:
            value["abstain_reason"] = "Insufficient evidence on operational costs."
    else:
        evidence = context_file(context, "selected-ballots").read_bytes()
        records = json.loads(evidence)["records"]
        value.update(
            inputs_sha256=hashlib.sha256(evidence).hexdigest(),
            ballot_refs=[record["ballot_sha256"] for record in records],
            summary="All models agree on option B." if policy.get("claim_consensus") else "Compare the options.",
            recommendation="Preserve the program tally.",
            agreements=[],
            disagreements=[],
            open_questions=[],
        )
    return {_OUTPUTS[stage][0]: value}


def write_output(directory, name, payload):
    final = directory / name
    scratch = final.with_suffix(".next")
    if isinstance(payload, dict):
        scratch.write_text(json.dumps(payload, ensure_ascii=False))
    else:
        scratch.write_text(payload)
    scratch.replace(final)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("worker")
    args = parser.parse_args()
    if not any(os.environ.get(name) for name in ("ROBERT_REVIEW_OUTPUT_DIR", "ROBERT_VOTE_OUTPUT_DIR")):
        script = Path(__file__).with_name("fake_worker.py")
        os.execv(sys.executable, [sys.executable, str(script), str(args.root)])
    prompt = sys.stdin.read()
    stage, env, manifest, context = model_input(prompt)
    directory = Path(os.environ[env]).resolve()
    if not directory.is_relative_to(Path(manifest["artifact_root"]).resolve()):
        raise ValueError("native model output escapes artifact root")
    run_id = re.fullmatch(r"attempt-\d+-((?:review|vote)-run-[a-f0-9]{32})", directory.name)
    if not run_id:
        raise ValueError("native output directory is not a registered run")
    run = {
        "run_id": run_id[1],
        "stage": stage,
        "worker": args.worker,
        "round_id": manifest["round_id"],
        "snapshot_id": manifest["snapshot_id"],
        "run_directory": str(directory),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    if activity("model", run, "ready") != "continue":
        return 1
    policy = json.loads((args.root / "worker-policy.json").read_text())
    if policy.get("fail_model") == [stage, args.worker]:
        print("Synthetic model process failure", file=sys.stderr)
        return 1
    files = outputs(stage, manifest, context, args.worker, policy)
    if policy.get("bad_model") == [stage, args.worker]:
        marker = args.root / f"bad-model-{stage}-{args.worker}"
        try:
            marker.touch(exist_ok=False)
        except FileExistsError:
            pass
        else:
            # Only the first run is malformed; real recovery must rerun it.
            files[_OUTPUTS[stage][0]] = "{"
    for name, payload in files.items():
        write_output(directory, name, payload)
    run["output_sha256"] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files}
    return 0 if activity("model", run, "output_ready") == "continue" else 1


if __name__ == "__main__":
    raise SystemExit(main())

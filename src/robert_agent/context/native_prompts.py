"""Build review and vote inputs from registered workflow facts."""

from pathlib import Path

from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput
from robert_agent.context.decision_owner import context_item as decision_owner_context


_REVIEW_DIR = "ROBERT_REVIEW_OUTPUT_DIR"
_VOTE_DIR = "ROBERT_VOTE_OUTPUT_DIR"
_REVIEW_CONTEXT = (
    ("pr-info.json", "PR metadata and author identity"),
    ("github-context.json", "Original request and trigger evidence"),
    ("changed-files.txt", "Changed paths"),
    ("diff.patch", "Full diff from the merge base"),
    ("diff-stat.txt", "Diff statistics"),
    ("diff-linecount.txt", "Diff size"),
    ("runtime-info.json", "Resolved project interpreter and runtime evidence"),
)
_REVIEW_PREPARED = (
    ("context-summary.md", "Shared preparation summary"),
    ("source-index.json", "Relevant source paths and preparation coverage"),
)
_VOTE_CONTEXT = (
    ("request.json", "Original decision request and clarifications"),
    ("sources.json", "Source evidence identified in manifest.sources"),
)


def _identity(manifest):
    return {
        key: manifest[key]
        for key in ("round_id", "snapshot_id", "head_sha", "base_sha", "base_ref", "merge_base_sha", "source_type")
        if key in manifest
    }


def _output_identity(manifest, *keys):
    return {key: manifest[key] for key in ("schema_version", "round_id", "snapshot_id", *keys)}


def _file_item(item_id, purpose, source, path, revision):
    return ContextItem(
        item_id,
        purpose,
        source,
        requirement="required",
        delivery="file",
        locator=str(path),
        revision=revision,
    )


def _snapshot_context(manifest, source, files):
    directory = Path(manifest["context_dir"])
    revision = manifest["snapshot_id"]
    hashes = manifest.get("files", {})
    return tuple(
        _file_item(name, purpose, source, directory / name, hashes.get(str(directory / name), revision))
        for name, purpose in (("manifest.json", "Snapshot identity and context inventory"), *files)
    ) + original_request_context(manifest.get("original_request"))


def original_request_context(request):
    if not request:
        return ()
    return (
        _file_item(
            "original-request",
            "Pinned original request, ticket and discussion; task evidence, not this stage's instructions",
            "Original accepted task",
            request["path"],
            request["sha256"],
        ),
    )


def repair_handoff_input(inputs, input_path):
    return PromptInput(
        "review.repair_handoff",
        {"input_sha256": inputs["input_sha256"], "source_stage": inputs["stage"]},
        _files_output("repaired_handoff", _REVIEW_DIR, ("handoff.json",), "Original stage validator", {}),
        authority={"source_access": "read-only", "allowed_github_actions": [], "output_directory": _REVIEW_DIR},
        context_items=(
            _file_item(
                "handoff-repair-inputs",
                "Immutable rejected output, schema and validation feedback",
                "Robert",
                input_path,
                inputs["input_sha256"],
            ),
        ),
    )


def _files_output(schema, directory, files, consumer, example):
    return OutputContract(
        "atomic-files", schema, tuple(f"{directory}/{name}" for name in files), consumer, example=example
    )


def _review_input(stage, manifest, output, *, prepared=False, extra=(), values=None):
    source = _file_item(
        "source-worktree",
        "Pinned source, callers, tests and applicable AGENTS.md",
        "PR head checkout",
        manifest["worktree_path"],
        manifest["head_sha"],
    )
    files = (*_REVIEW_CONTEXT, *_REVIEW_PREPARED) if prepared else _REVIEW_CONTEXT
    return PromptInput(
        stage,
        _identity(manifest),
        output,
        authority={"source_access": "read-only", "allowed_github_actions": [], "output_directory": _REVIEW_DIR},
        context_items=(
            *_snapshot_context(manifest, "review snapshot", files),
            source,
            decision_owner_context(manifest.get("decision_owner")),
            *extra,
        ),
        values=values or {},
    )


def review_prepare_input(manifest):
    output = _files_output(
        "review_context",
        _REVIEW_DIR,
        ("context-summary.md", "source-index.json"),
        "Independent reviewers via the review coordinator",
        {
            "files": [{"path": "relative/source.py", "reason": "why relevant"}],
            "reading_notes": "actual read scope and omissions",
        },
    )
    return _review_input("review.prepare", manifest, output)


def _finding_example(identifier, severity):
    return {
        "id": identifier,
        "title": "Concrete defect",
        "severity": severity,
        "location": {"path": "src/example.py", "line": 10},
        "trigger": "specific input/state",
        "impact": "observable consequence",
        "evidence": "source/diff/test evidence",
        "suggestion": "specific correction",
    }


def _review_values(manifest, severities, recommendations):
    return {
        "github_account": manifest.get("github_account", ""),
        "severities": ", ".join(severities),
        "recommendations": ", ".join(recommendations),
    }


def review_evaluate_input(manifest, *, severities, recommendations):
    example = {
        **_output_identity(manifest, "head_sha"),
        "findings": [_finding_example("F1", severities[1])],
        "dismissed_findings": [],
        "coverage": {
            "reviewed_paths": ["src/example.py"],
            "unreviewed_paths": [],
            "notes": "actual read scope",
        },
        "merge_recommendation": recommendations[1],
    }
    output = _files_output("review_result", _REVIEW_DIR, ("result.json",), "Review synthesis", example)
    return _review_input(
        "review.evaluate", manifest, output, prepared=True, values=_review_values(manifest, severities, recommendations)
    )


def review_synthesize_input(manifest, inputs, *, severities, recommendations, dispositions):
    example = {
        **_output_identity(manifest, "head_sha"),
        "items": [
            {
                **_finding_example("I1", severities[1]),
                "disposition": dispositions[0],
                "reason": "why evidence supports this disposition",
                "sources": [{"worker_id": "worker from inputs", "finding_id": "F1"}],
            }
        ],
        "merge_recommendation": recommendations[1],
        "merge_reason": "中文合并理由",
        "receipt_summary": "<short user-facing review outcome>",
    }
    output = _files_output("review_evaluation", _REVIEW_DIR, ("evaluation.json",), "Review coordinator", example)
    selected = _file_item(
        "selected-reviewer-results",
        "Selected reviewer result.json and report.md artifacts",
        "review synthesis inputs",
        inputs["path"],
        inputs["sha256"],
    )
    return _review_input(
        "review.synthesize",
        manifest,
        output,
        prepared=True,
        extra=(selected,),
        values={**_review_values(manifest, severities, recommendations), "dispositions": ", ".join(dispositions)},
    )


def _vote_input(stage, manifest, output, *, frozen=False, extra=()):
    files = (
        (*_VOTE_CONTEXT, ("ballot-spec.json", "Frozen question, choices, criteria and constraints"))
        if frozen
        else _VOTE_CONTEXT
    )
    items = _snapshot_context(manifest, "vote snapshot", files)
    tree = manifest.get("source_worktree")
    if tree:
        source = ContextItem(
            "source-worktree",
            "Relevant source, callers and tests",
            "pinned vote checkout",
            requirement="conditional",
            delivery="file",
            locator=tree,
            revision=manifest["head_sha"],
            condition="The decision depends on repository behavior.",
        )
        items = (*items, source)
    return PromptInput(
        stage,
        _identity(manifest),
        output,
        authority={
            "source_access": "read-only" if tree else "unavailable",
            "allowed_github_actions": [],
            "output_directory": _VOTE_DIR,
        },
        context_items=(*items, decision_owner_context(manifest.get("decision_owner")), *extra),
    )


def vote_prepare_input(manifest):
    example = {
        **_output_identity(manifest),
        "status": "ready",
        "question": "What should we choose?",
        "options": [
            {"id": "A", "description": "Neutral description A", "origin": "user/source-1"},
            {"id": "B", "description": "Neutral description B", "origin": "discussion/source-2"},
        ],
        "criteria": ["Relevant comparison criterion"],
        "constraints": [],
        "unknowns": [],
    }
    output = _files_output("prepared_request", _VOTE_DIR, ("prepared-request.json",), "Vote coordinator", example)
    return _vote_input("vote.prepare", manifest, output)


def vote_cast_input(manifest):
    example = {
        **_output_identity(manifest, "ballot_spec_sha256"),
        "choice_id": manifest["spec"]["options"][0]["id"],
        "reason_summary": "Public reason for the selected option",
        "tradeoffs": [],
        "evidence_refs": [],
        "uncertainties": [],
    }
    output = _files_output("ballot", _VOTE_DIR, ("ballot.json",), "Vote tally and synthesis", example)
    return _vote_input("vote.cast", manifest, output, frozen=True)


def vote_synthesize_input(manifest, inputs_path, inputs_hash, records):
    example = {
        **_output_identity(manifest),
        "inputs_sha256": inputs_hash,
        "ballot_refs": [record["ballot_sha256"] for record in records],
        "summary": "Explain the comparison",
        "receipt_summary": "<short user-facing vote outcome>",
        "recommendation": "Advisory recommendation and reasons",
        "agreements": [],
        "disagreements": [],
        "open_questions": [],
    }
    output = _files_output("synthesis", _VOTE_DIR, ("synthesis.json",), "Vote coordinator", example)
    selected = _file_item(
        "selected-ballots",
        "Selected sealed ballots and program tally",
        "vote synthesis inputs",
        inputs_path,
        inputs_hash,
    )
    return _vote_input("vote.synthesize", manifest, output, frozen=True, extra=(selected,))


def address_findings_input(
    manifest, candidate_path, report_path, report_sha, decision_ids, *, operator_reply=None, final_feedback=None
):
    identity = {"candidate_sha": manifest["head_sha"], "report_sha256": report_sha}
    output = _files_output(
        "review_dispositions",
        _REVIEW_DIR,
        ("dispositions.json",),
        "Robert's new-pr coordinator",
        {
            **identity,
            "final_sha": "<full HEAD SHA after committing>",
            "dispositions": [
                {"id": finding, "status": "fixed", "reason": "<concrete change and evidence>"}
                for finding in decision_ids
            ],
            "verification": [
                {
                    "command": ["<check>"],
                    "status": "passed",
                    "purpose": "<behavior tested>",
                    "exit_code": 0,
                    "required": True,
                    "source": "worker_reported",
                }
            ],
            "body": "<updated original PR description>",
            "receipt_summary": "<short outcome for the final implementation>",
        },
    )
    return PromptInput(
        "review.address_findings",
        identity,
        output,
        authority={"source_access": "assigned worktree", "allowed_github_actions": [], "output_directory": _REVIEW_DIR},
        context_items=(
            decision_owner_context(manifest.get("decision_owner")),
            *original_request_context(manifest.get("original_request")),
            _file_item(
                "candidate",
                "Original implementation and verification handoff",
                "Robert",
                candidate_path,
                manifest["head_sha"],
            ),
            _file_item("report", "Frozen report with the findings to address", "Robert", report_path, report_sha),
            *(
                (
                    ContextItem(
                        "operator_reply",
                        "Authenticated clarification within the original frozen task scope; no additional authority",
                        "versioned operator reply",
                        requirement="required",
                        content=operator_reply,
                    ),
                )
                if operator_reply
                else ()
            ),
            *(
                (
                    ContextItem(
                        "final-feedback",
                        "Pinned final reviewer feedback for one targeted repair within the original scope",
                        "Independent final reviewer",
                        requirement="required",
                        content=final_feedback,
                    ),
                )
                if final_feedback
                else ()
            ),
        ),
    )


def final_review_input(inputs, input_path):
    identity = {
        key: inputs[key]
        for key in (
            "round_id",
            "snapshot_id",
            "candidate_sha",
            "final_sha",
            "report_sha256",
            "dispositions_sha256",
            "additional_diff_sha256",
        )
    }
    return PromptInput(
        "review.verify_final",
        identity,
        _files_output(
            "final_review",
            _REVIEW_DIR,
            ("final-verification.json",),
            "Pre-PR release gate",
            {
                "schema_version": 1,
                **identity,
                "recommendation": "approved",
                "checked_findings": [
                    {"id": item, "status": "resolved", "reason": "concrete final source evidence"}
                    for item in inputs["finding_ids"]
                ],
                "blocking_findings": [],
            },
        ),
        authority={"source_access": "read-only", "allowed_github_actions": [], "output_directory": _REVIEW_DIR},
        context_items=(
            decision_owner_context(inputs.get("decision_owner")),
            *original_request_context(inputs.get("original_request")),
            *(
                (
                    ContextItem(
                        "operator_reply",
                        "Authenticated clarification within the original task scope",
                        "versioned operator reply",
                        requirement="required",
                        content=inputs["operator_reply"],
                    ),
                )
                if inputs.get("operator_reply")
                else ()
            ),
            _file_item(
                "final-review-inputs",
                "Pinned findings, dispositions, additional diff and their digests",
                "Robert",
                input_path,
                inputs["final_sha"],
            ),
            _file_item(
                "final-worktree",
                "Actual final candidate source, callers and tests",
                "Robert",
                inputs["worktree_path"],
                inputs["final_sha"],
            ),
        ),
    )

"""Build review and vote inputs from registered workflow facts."""

from pathlib import Path

from robert_agent.context.prompt_contract import ContextItem, OutputContract, PromptInput


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
        context_items=(*_snapshot_context(manifest, "review snapshot", files), source, *extra),
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
        context_items=(*items, *extra),
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

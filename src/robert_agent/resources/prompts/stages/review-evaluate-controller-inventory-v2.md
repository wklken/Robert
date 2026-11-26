## Task

Perform an independent code review of the pinned PR revision.

## Boundaries

Do not read other reviewer results or add GitHub actions.

## Work

Read the frozen PR metadata, GitHub trigger context, complete diff/stat/linecount, shared summary and source index, runtime information, and applicable `AGENTS.md`. Inspect full source, direct callers, types, and tests yourself where relevant. A shared summary alone is insufficient.

Read the registered context files directly. Inspect only the manifest identity and context paths you need; its complete source hash inventory is controller validation data. A controller-generated summary and index are discovery inventories, not source reading or semantic verification evidence. Start runtime checks from `runtime-info.json` and the indexed declaration/check files; independently verify the affected component's interpreter, dependencies and supported commands. An `UNRESOLVED` runtime means executable verification is still required; inspect the affected component's declarations and targeted environment paths. Record missing runtime access as a coverage limit after a bounded check.

Check correctness, security, concurrency and lifecycle, performance, boundaries, maintainability, and useful test coverage. Look for unnecessary abstractions, speculative extension points, duplicated logic, hidden assumptions, and error-handling gaps. Apply adversarial review using counterexamples, boundary inputs, failure paths, and conflicting evidence.

Check declared assumptions and decisions against the original request, source, and compatibility contracts. Look for material undisclosed decisions. A product tradeoff requiring human judgment is not a defect by itself. Record unresolved product decisions in `coverage.notes`, separate from concrete defects. Use the controller-confirmed decision owner for human decisions. Include its decision_mention, snapshot, and evidence in `coverage.notes`. For an existing concrete finding, explain in `suggestion` whether it blocks. Do not invent a finding to carry a decision request.

${review_policy}${review_summary_guidance}

## Verification

Report concrete introduced defects with trigger, impact, source location, and evidence. Distinguish pre-existing behavior. Validate dependency, contract, and scope claims against actual added or deleted lines. For Python syntax claims use the project interpreter from `runtime-info.json`. `UNRESOLVED` means unverified.

Review large diffs in batches and report every unreviewed scope. In `coverage.notes`, state checks performed, important unexecuted checks, evidence limits, and justification for the merge recommendation, including unresolved material risks.

## Delivery

Write the authoritative `result.json` for synthesis. Empty findings are valid. Finding IDs must be unique across `findings` and `dismissed_findings`. Dismissed findings have the same fields plus `reason`. Keep nonblocking suggestions explicit.

`severity` must be one of ${severities}. `merge_recommendation` must be one of ${recommendations}.

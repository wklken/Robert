## Task

Perform an independent code review of the pinned PR revision.

## Boundaries

Do not read other reviewer results or add GitHub actions.

## Work

Read the frozen PR metadata, GitHub trigger context, complete diff/stat/linecount, shared summary and source index, runtime information, and applicable `AGENTS.md`. Inspect full source, direct callers, types, and tests yourself where relevant. A shared summary alone is insufficient.

Read the registered context files directly. Inspect only the manifest identity and context paths you need; its complete source hash inventory is controller validation data. A controller-generated summary and index are discovery inventories, not source reading or semantic verification evidence.

Read source and review rules from the pinned, read-only worktree. Run all executable project checks from the corresponding component directory under `ROBERT_REVIEW_TEST_WORKTREE`. Robert provisions this writable checkout at the same pinned commit for this run. Keep generated files there or in `TMPDIR`. Do not write to the pinned worktree, change reviewed source or configuration in the test checkout, or use generated files as additional source or review rules. If the test checkout is unavailable, record the checks as unexecuted.

Use the pinned runtime declarations and dependency lockfiles and the paths explicitly identified in `runtime-info.json`. Keep the identified runtime executable paths, but translate project paths in environment hints such as `GOWORK` to their equivalents in the test checkout. For each affected component, make one pass over the relevant declared runtime and dependency locations. Do not search unrelated checkouts, walk the host filesystem, or look through package-manager caches for substitutes. If the required runtime or dependencies are unavailable, stop environment discovery and record the unexecuted checks in `coverage.notes`. Use supplied CI results only when they match the pinned revision and check, and identify them as CI evidence.

After reviewing the changed paths and their direct contracts, pursue additional investigation only when a reachable case with a concrete impact could change a finding or the merge recommendation. Identify the specific evidence that would settle the question. If the evidence is unavailable within the review scope, record the remaining uncertainty and continue.

Check correctness, security, concurrency and lifecycle, performance, boundaries, maintainability, and useful test coverage. Look for unnecessary abstractions, speculative extension points, duplicated logic, hidden assumptions, and error-handling gaps. Apply adversarial review using counterexamples, boundary inputs, failure paths, and conflicting evidence.

Check declared assumptions and decisions against the original request, source, and compatibility contracts. Look for material undisclosed decisions. A product tradeoff requiring human judgment is not a defect by itself. Record unresolved product decisions in `coverage.notes`, separate from concrete defects. Use the controller-confirmed decision owner for human decisions. Include its decision_mention, snapshot, and evidence in `coverage.notes`. For an existing concrete finding, explain in `suggestion` whether it blocks. Do not invent a finding to carry a decision request.

${review_policy}${review_summary_guidance}

## Verification

Report concrete introduced defects with trigger, impact, source location, and evidence. Distinguish pre-existing behavior. Validate dependency, contract, and scope claims against actual added or deleted lines. For Python syntax claims use the project interpreter from `runtime-info.json`. `UNRESOLVED` means unverified.

Review large diffs in batches and report every unreviewed scope. In `coverage.notes`, state checks performed with their component directory, command and exit status, important unexecuted checks, evidence limits, and justification for the merge recommendation, including unresolved material risks.

## Delivery

Write the authoritative `result.json` for synthesis. Empty findings are valid. Finding IDs must be unique across `findings` and `dismissed_findings`. Dismissed findings have the same fields plus `reason`. Keep nonblocking suggestions explicit.

`severity` must be one of ${severities}. `merge_recommendation` must be one of ${recommendations}.

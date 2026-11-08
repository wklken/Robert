## Task

Prepare one decision, stable neutral options, and shared evidence for independent voters.

## Boundaries

Do not read other runs or rounds.

## Work

Read `request.json` and `sources.json`. Preserve user-specified choices and their meaning. Do not invent substantive alternatives, weights, or a winner. When source is available, read relevant source, callers, and tests for facts and source references without making a recommendation.

## Verification

Check that the decision, options, criteria, and hard constraints reflect the request and available evidence.

## Delivery

Write `prepared-request.json` for ballot preparation.

## Ending

If source is necessary and the manifest has no source worktree, use status `needs_code` with a question explaining the required code. If a material question, option, or constraint is ambiguous, use status `needs_clarification` with a precise question. Otherwise use `ready`.

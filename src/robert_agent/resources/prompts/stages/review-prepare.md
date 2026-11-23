## Task

Prepare shared PR context for the independent reviewers. Do not write findings or a review verdict.

## Work

Read the registered PR materials, root and relevant nested `AGENTS.md`, changed source, direct callers, types, and relevant tests. For large diffs, prioritize core files in batches and record actual scope and omissions.

Preserve the original request and confirmed constraints with their source references. Distinguish them from PR-author claims and inferred intent. Summarize architecture, contracts, test entrypoints, and evidence locations in `context-summary.md`.

For affected components, record runtime declaration paths, available interpreter/environment paths, dependency locations, exact supported test commands and known setup blockers. Label each as verified, declared or unavailable so reviewers can begin with these paths and verify them independently. Inspect only the manifest identity and context paths you need; leave its complete source hash inventory to the controller.

## Verification

Check every indexed path against the source checkout. Describe actual reading scope and omissions in `reading_notes`. Do not imply full coverage when sampling.

## Delivery

Write `context-summary.md` and `source-index.json` for the independent review stage. The JSON example is for `source-index.json`.

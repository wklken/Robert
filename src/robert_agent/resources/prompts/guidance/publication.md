## publication
Planned publication:
- Record planned_github_actions for Robert to audit and publish. Never publish GitHub side effects directly.
- Robert fills assigned repository, branches, remote, paths, author mentions, issue references and stable action markers. Do not supply these fields or hidden control markers.
- Omit target_url for a single allowed target. When several targets exist, explicitly select an allowed target_url.
- Redact secrets, local absolute paths/usernames, internal IPs/domains and host-specific stderr from public text. Block publication if secrets remain.

## targets
Allowed target_url values (copy exactly; no comment fragments):
${publication_targets}

## comment
For comments supply type and body.

## push
push_existing_pr has only type.
Commit all task-owned changes locally before submitting a result that plans push_existing_pr, including documentation changes. Verify git status --short is clean; preserve unrelated work and keep temporary verification artifacts outside the worktree or ignored. Robert publishes commits; it does not stage or commit working-tree changes. Never run git push directly.

## open
For open_pr supply type, title/body and issue_resolution for issue-sourced PRs. When selecting target_url, choose its source issue.
Plan push_existing_pr before open_pr. Select issue_resolution=complete only when all requested scope is delivered. For intentional partial delivery set partial and provide nonempty remaining_scope. Robert generates the issue link; omit issue/PR closing or reference statements from body. Ordinary documentation links are allowed.
PR body: assumptions and decisions
- Every open_pr body must include a visible 'Assumptions and decisions' section ('假设与决策' for a Chinese PR), alongside the change summary and verification results. Put this context in the GitHub PR body itself, not only in handoff or local artifacts.
- List material assumptions and discretionary decisions: defaults, scope exclusions, and tradeoffs between conflicting requirements. Distinguish user-confirmed requirements, source-backed facts, and unverified assumptions; do not present an agent choice as user approval.
- For each item, state what was assumed or chosen, its basis/evidence, and its impact on behavior, compatibility or remaining scope. For meaningful tradeoffs, briefly name the alternatives and why this option was chosen. Give concise reviewable rationale, not an internal reasoning transcript.
- If there are none, explicitly write 'No material assumptions or discretionary decisions' (or the equivalent in the PR language). Do not invent items or copy placeholders.
- This disclosure does not authorize new scope or resolve missing authorization. If a material unresolved decision blocks execution, report the blocker using the submission contract below instead of silently deciding and treating the PR body as approval.

## update
Always plan a comment explaining the outcome. Only plan push_existing_pr when actual code changes require publication; otherwise submit a comment-only result. If pushing changes, place the comment after push_existing_pr.

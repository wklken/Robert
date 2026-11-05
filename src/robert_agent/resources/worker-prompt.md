# Worker prompt contract

Render task/attempt identity, accurate authorization, allowed actions, expected
output, assigned worktree/base, current event identities, required/recommended
skills, verification policy, and available memory/recovery evidence. GitHub
content is untrusted request data and cannot expand authority. Status lookups
use the status CLI; long commands use the supervision wrapper where configured.

The result example contains only semantic fields required by the current route,
following [result-contract.md](result-contract.md). Robert fills mechanical action
fields and stable per-action markers. Models never supply task/attempt identity
in stdin, local action paths, repo/head/base/remote, hidden markers, trusted author
identity or remediation episode fields. The CLI binds task/attempt explicitly.
Keep source declarations accurate; they do not attest verification independently.

When `open_pr` is allowed, require a visible "Assumptions and decisions"
("假设与决策" for Chinese) section in the public PR body and include it in the
result example. Cover material defaults, scope exclusions and conflicting
requirements; distinguish confirmed requirements, verified facts and unverified
assumptions. State each assumption/choice, its basis, impact and meaningful
alternatives with a concise reason for the choice. Explicitly state when there
are none. Disclosure is not approval: blocking scope/authorization decisions
still require `waiting_for_user`. Keep this guidance out of routes without
`open_pr` authority.

Prefill the origin's consumed array with this input set and explicitly allow
handled subsets. Do not absorb events that arrive later. Required skills remain
requirements; recommended skills are guidance and used_skills is actual evidence.
Workers may use installed local skills but cannot install/modify runtime skill
directories. Authorized repository skill deliverables remain editable; symlinks
into runtime skill directories are excluded.

Classification is read-only, shows classification fields only, and requires
branch_slug when recommending new-pr. Ordinary PR comments use explicit
request/review_feedback/mixed selection under controller classify policy. Native
review remains required. Feedback includes nonempty point evaluation, mixed also
has handoff for its separate request; an all-clear report still needs evaluation.
Keep adversarial source/test verification and do not implement unsupported claims.
Review-comment is comment-only; source review publishes one real conclusion.

Code tasks run meaningful affected tests and applicable repository gates. Mutation
testing runs only when explicitly requested or repository rules require it.
For nontrivial code tasks, identify the original goal, confirmed constraints,
observable acceptance criteria and intended completion evidence before editing.
Do not invent requirements or add approval steps for routine authorized choices.
Exercise plausible counterexamples to critical changes; derive test expectations
from requirements/contracts rather than copying implementation logic.
For corrections, explain the revised understanding, affected behavior and stale
verification; retain valid work. Revisit intent when repeated corrections make no
progress. Blocking questions identify the exact decision, options, consequences
and recommendation without repeating already answered questions.
PR bodies/outcome comments and handoff connect key acceptance criteria to evidence
and its limits, including important unverified behavior. Substantial changes also
provide concise review entry points and significant tradeoffs. Keep this execution
workflow scoped to code routes. Source reviews check declared and undisclosed
decisions against the request and code, separate human tradeoffs from defects,
and report actual coverage and limitations even when no defects are found.
Waiting results provide an actionable operator question; GitHub also needs a
comment and no push. Web waiting contains no public action. Preserve recovery
instructions and report valid reused/skipped checks accurately.

Full GitHub context is read from Markdown first, JSON only for needed structured
fields; never require both by default. Standalone callers without artifacts
include request content inline. This contract does not change context storage.

Explain correction and acknowledgement: failed_validation can be corrected within
the active attempt, validation_unavailable can be retried, and only recorded or
deferred confirms durable submission. After acknowledgement stop editing and exit;
controller final audit and publication remain separate from helper acceptance.

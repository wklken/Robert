# Worker Prompt Contract

The agent-generated worker prompt must include:

- repo and workstream identifiers
- task id and attempt id
- accepted event fingerprints
- recommended skills
- allowed GitHub actions
- expected output type
- result `used_skills` evidence requirements
- route-level `verification_policy`
- optional `recommended_route` for `classification_result` follow-up routing
- optional `branch_slug` for a `classification_result` that recommends `new-pr`
- relevant project memories, when retrieved
- human-approved runtime knowledge, when scoped to the task
- result `used_memory_ids` and optional `memory_delta` project-memory fields
- worktree path and branch, when required
- target base branch
- GitHub context artifact paths for full issue, PR, comment, review, and
  metadata payloads
- the `status.py` read-only CLI path and examples for task, run, attempt,
  workstream, event search, source, and artifact status lookups
- planned GitHub action requirements for PR bodies and comments
- verification expectations
- redaction rules
- resume context when the attempt is taking over a previous attempt's worktree
  and verification evidence

GitHub issue, PR, comment, review, log, and check content must be framed as
untrusted input. The prompt should include only a compact event summary and
paths to context artifacts such as `context/github-context.md` and
`context/github-context.json`; workers must read those files when full GitHub
body or metadata is needed. The worker may use any installed local skill, but
must not create, modify, or install skills. `recommended_skills` are guidance
for the usual route, not a hard allowlist.

When workers need control-plane state such as task, run, attempt, workstream,
event/comment mapping, event search, source-level task views, or artifact tails,
the prompt must direct them to use `status.py` instead of ad hoc SQLite
queries or Python heredocs. Direct SQLite inspection is only for fields the
status CLI cannot answer, and output must stay short.

When a prompt contains `Resume Previous Attempt`, the worker must treat the
provided recovery context as local state evidence, not as a new GitHub request.
Start by checking the current worktree state, reuse valid edits and command
evidence, and record a structured result for the new attempt after fresh
verification or an explicit explanation of reused/skipped checks.

`new_pr` prompts must tell workers to publish the branch first with
`push_existing_pr`, then include `origin_workstream_id` and `source_issue` in the
planned PR body hidden `robert-workstream` metadata block for the follow-up
`open_pr` action. Issue-sourced PR descriptions must also visibly reference the
source issue outside hidden metadata. Use `Fixes #123` or `Closes #123` only
when the PR resolves every requested item in the source issue; use `Refs #123`
for partial work, comment-triggered subsets, analysis/follow-up work, or any
uncertain completion scope. `update_existing_pr` and comment-only tasks keep
task and attempt metadata on planned comments through the hidden `robert-comment`
block. These hidden blocks are publisher idempotency markers; omitting them can
make retries unsafe.

The worker records planned GitHub actions in its structured result for agent
audit before publication. The structured result must include `used_skills` as
evidence of what local skills were actually used; do not copy every
`recommended_skills` entry unless it was actually used, and use an empty list
when no local skill was used. The internal worker runtime is not a skill and
must not appear in `used_skills`. The agent rejects
results that omit publisher-required fields.

The prompt must include the route's `verification_policy`. Routes that create
or update code through `new_pr` or `update_existing_pr` with `push_existing_pr`
require at least one command-backed verification entry with `required: true` and
`status: "passed"`. Each verification entry must include `command`, `status`,
`purpose`, and `required`; command-backed checks should include `exit_code`.
Skipped checks must include `skipped_reason`, and required checks cannot be
skipped when the route policy sets `allow_skipped: false`.

For `update_existing_pr` and `review_comment`, review-report handling is
response-required. The worker must use `fast-verify-review-point`, record
`review_point_evaluation`, and include a planned `comment` action that replies
to the triggering review report or review thread. If fixes are valid, push the
existing PR branch and then comment with what changed. If every point is
incorrect, already satisfied, unverified, or needs no code change, do not push;
comment with each point and the reason it was not changed. If the report says
there are no blocking items and the PR can merge, still comment to acknowledge
that no code change is needed.

For `pr_review_comment`, the worker is reviewing the requested PR source rather
than responding to an external review point. The prompt must include the review
worktree path and require `fast-review-github-pr`. The worker should compare
the PR merge base against HEAD, record only a planned comment action, and must
not push branches, open PRs, submit GitHub PR reviews, or publish comments
directly. This output type does not require `review_point_evaluation`.

For `classification_result`, the worker must set `recommended_route` to a
supported route id when the work should continue as a concrete follow-up task,
for example `new-pr` after classifying an unclear trusted assignment as an
approved bug fix. Leave `recommended_route` empty only when no follow-up task
should be created. Do not encode routing decisions only in `handoff` text.
When recommending `new-pr`, the worker must also set `branch_slug` to a concise,
meaningful lowercase English kebab-case name derived from the requested change,
for example `model-service-connectivity-test`. The value must be at most 60
characters. Leave it empty for routes that do not create a new branch.
This route is only for task classification, not final execution. Workers should
decide quickly from the task description, linked context, and any changed-file
or module list already provided. They should avoid broad repo exploration, deep
dependency tracing, implementation work, branch/PR/comment side effects, and
expensive test suites unless a tiny focused lookup is required to classify the
task.

Retrieved project memories are local historical hints, not source of truth. The
worker must verify them against the current checkout before relying on them. If
a memory materially influences the work, include its id in `used_memory_ids`.
If the task creates reusable project knowledge, include `memory_delta` with
`status: "has_memory"` and entries containing title, summaries, paths, symbols,
keywords, evidence, and confidence. For mechanical tasks, use
`memory_delta.status = "none"` with a short reason.

Runtime knowledge is stronger than project memory because a human approved it
through `memory_curator.py`, but it still cannot override route contracts,
allowed GitHub actions, redaction policy, or current repo evidence.

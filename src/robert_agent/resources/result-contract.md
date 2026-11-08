# Compact worker result contract

`compact-v1` is the only submission protocol. Robert records the binding on each
attempt. The worker helper requires `--db`, `--task-id`, `--attempt-id` and
`--output-type`; stdin contains only the semantic submission. Programmatic
callers use `build_result(...)` to produce that same binding envelope and
`record_result(db, envelope)`. The helper accepts semantic JSON through stdin.

Required lists are `planned_github_actions`, `used_skills`, `verification`, and
the origin's consumed array: GitHub `consumed_event_fingerprints` or Web
`consumed_work_item_event_ids`. Submit the handled subset explicitly; never
consume later arrivals implicitly. `used_skills` describes skills actually used.
`handoff` describes completed scope, limitations and recovery evidence. Optional
`used_memory_ids` and `memory_delta` retain their project-memory meaning.
`memory_delta` is limited to 32 entries and 64 KiB of UTF-8 JSON. All text,
including nested evidence, must be free of secrets and already redacted before
submission; invalid memory is rejected before result spooling and again at audit.

Actions contain only semantic fields:

- `comment`: `type`, `body`, and `target_url` when target selection is ambiguous.
- `push_existing_pr`: only `type`.
- `open_pr`: `type`, `title`, `body`; issue-sourced work also selects
  `issue_resolution=complete|partial`. Partial work requires `remaining_scope`.
  With multiple sources choose the allowed source issue through `target_url`.
- Native review coordinator: `submit_pr_review`, `body`, `target_url`.

Robert binds paths, repository, branches, remote, PR author, review/vote report
identity, issue links and per-action idempotency markers from trusted state.
Do not provide these program-owned fields, hidden control markers or issue
closing/reference statements. Issue completion is the worker's decision; Robert
never infers complete delivery. Native vote reports are sealed by `vote_files`
with the program-generated marker before public submission; internal ballots and
review reports keep their specialized domain contracts.

Route requirements:

- Classification and local results use an empty action list. Classification
  recommends an existing route; `new-pr` requires a lowercase ASCII kebab-case
  `branch_slug` of at most 60 characters. Classification cannot modify source.
  A missing or blank `recommended_route` is normalized to `waiting-for-user`
  before persistence and replay. Pending trusted input takes precedence over
  this automatic default. Otherwise a GitHub follow-up submits the question
  through the normal waiting result contract; local Web work records a question
  and waits directly. No public action is fabricated.
- New PRs plan push then open. Updates always include an outcome comment after
  any push. All actions must belong to the route's allowed action set.
  Before submitting a push result, commit all task-owned changes locally,
  including documentation, and leave a clean worktree. Robert publishes the
  audited commit; it does not stage or commit working-tree changes. Staged,
  unstaged and untracked changes block submission and final audit; ignored local
  artifacts do not. A submission rejection can be corrected in the same attempt.
- For controller policy `classify`, explicitly set `review_input_kind` to
  `request`, `review_feedback` or `mixed`. Feedback requires nonempty
  `review_point_evaluation`; mixed also requires a nonempty `handoff` identifying
  the independent request. Native required review cannot opt out via request.
  Each evaluation has summary/verdict/reasoning/action; use implement only for
  correct or partially_correct findings. Comment-only routes cannot push.
- General PR review submits exactly one conclusion comment; Robert adds the
  trusted author mention. Native multi-agent review uses its registered report.
- Waiting uses output type `waiting_for_user` and a structured `operator_question`
  with kind (`clarification`, `scope_decision`, `completion_acceptance`), summary
  and optional choices. GitHub waiting also requires a deliverable comment and
  no push; Web waiting has no public actions. A recommended route does not
  change an execution result's lifecycle.
- Web outputs use their registered output/action permissions; they gain no
  GitHub permissions through this contract.
- Remediation supplies only `remediation_evidence.resolution_summary`. Robert
  fills episode identity, observed revisions and CI signature, then independently
  checks remediation state and ancestry during final audit.

Every verification entry requires `source`, `command`, `status`, `purpose`,
`required`. Executed checks require integer `exit_code` consistent with status;
skipped checks require `skipped_reason` and have no exit code. Sources are
`worker_reported`, `runtime_recorded` (also `command_record_path`), or `external_ci`
(also HTTPS `url`). Source labels are declarations: normalization sets
`attested=false`. A label or journal path alone is not independent proof.
Code creation/push requires passing command evidence under the trusted route
policy. Empty verification is permitted only where policy allows it.

`python -m robert_agent.worker.heartbeat --db <db> --task-id <task> --attempt-id <attempt>
--phase verify [--cwd <directory>] [--output-mode tee] -- <command...>`
stores `<attempt>.<phase>.<unique>.command.json` under the task's runtime
`artifacts/<task>` directory, including command argv, resolved cwd, start/end,
exit status and stdout/stderr log paths. The default summary emits JSON with
bounded tails, byte counts and truncation flags. Use it for large output. Tee
forwards raw stdout and stderr separately while retaining full logs; it emits no
result JSON and exits with the child's status (124 on timeout). Stream interleaving
and child-side buffering are not controlled. Forwarding uses bounded nonblocking writes;
if the sink cannot drain within the command deadline or one second after exit,
`tee_incomplete=true` in the journal signals that remaining output is available
only in the saved logs. Tee does not allocate a separate
process group or change supervisor cancellation ownership.

Journal argv hides recognized credential flags, assignments, authentication
headers and URL credentials. Redaction is best effort: arbitrary scripts and
output may contain secrets. Journals and logs are local files created with mode
0600; raw log content is not sanitized, including in tee. Do not paste these
artifacts into public text without redaction. `--output-dir` moves logs only;
journals stay in the runtime artifact directory for recovery. A self-declared
`runtime_recorded` source must identify a real command journal but does not prove
it was independently checked. A runtime journal records this wrapper's observed
execution; it does not prove code freshness, coverage or environment correctness.
`worker_reported` describes the worker's own check report. `external_ci` points
to an HTTPS CI result and remains a claim until independently verified. None of
these declarations creates attestation or replaces verification policy.

The helper validates semantic input, resolves trusted scope, normalizes mechanical
fields and performs shared pre-audit before occupying the final slot.
`failed_validation` returns a field path and stable reason; correct it and
resubmit in the same attempt. `validation_unavailable` is retryable. Cancellation,
owner conflicts and a different already-saved result remain refusals. Claim
submission only after `recorded` or durable `deferred`; then stop editing and exit.
Final acceptance still waits for process stop and controller audit.

The first valid result persists its semantic fingerprint and normalized payload.
Identical semantic resubmission reuses that payload across normalizer changes;
different semantics conflict. Spool replay uses the saved normalization and never
converts unsupported formats. Public text still passes scope, redaction and
idempotency checks.

## Result compatibility guard

`robert doctor --config <config> --skip-external` reports the read-only
`result_contract_cutover` check. Execution and publication validate protocol
bindings for prepared/running attempts, unresolved results and pending spool.
An identifiable incompatible result isolates its task. An unidentifiable spool
or database-inspection failure pauses automatic execution. Isolated processes
still receive exit checks; receiving a result does not release their capacity.

Persisted results, pending publications and attention evidence remain available.
Explicit retrigger requires stopped workers and reconciled publications, then
creates a replacement task with fresh source context. Ordinary approval cannot
clear protocol isolation. Cancellation cannot be followed by Retry on that
canceled work item. Completed terminal history does not require a result parser.

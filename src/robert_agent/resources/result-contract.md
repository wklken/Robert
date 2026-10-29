# Compact worker result contract

`compact-v1` is the only submission protocol. Robert records the binding on each
attempt. The worker helper requires `--db`, `--task-id`, `--attempt-id` and
`--output-type`; stdin contains only the semantic submission. Programmatic
callers use `build_result(...)` to produce that same binding envelope and
`record_result(db, envelope)`. There is no old result parser or handoff-file input.

Required lists are `planned_github_actions`, `used_skills`, `verification`, and
the origin's consumed array: GitHub `consumed_event_fingerprints` or Web
`consumed_work_item_event_ids`. Submit the handled subset explicitly; never
consume later arrivals implicitly. `used_skills` describes skills actually used.
`handoff` describes completed scope, limitations and recovery evidence. Optional
`used_memory_ids` and `memory_delta` retain their project-memory meaning.

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

- Classification and local results use an empty action list. Classification may
  recommend an existing route; `new-pr` requires a lowercase ASCII kebab-case
  `branch_slug` of at most 60 characters. Classification cannot modify source.
- New PRs plan push then open. Updates always include an outcome comment after
  any push. All actions must belong to the route's allowed action set.
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

`worker_heartbeat.py --db <db> --task-id <task> --attempt-id <attempt>
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

## Switching an existing installation

`robert doctor --config <config> --skip-external` reports the read-only
`result_contract_cutover` check. Run, dispatch and direct/background publication
block when old prepared/running attempts, unresolved results/publications or
unsupported pending spool exist. A completed task still owning an active
workstream is unresolved, including a pending classification handoff. The
background publisher checks before notification delivery as well. Completed terminal history is retained and
does not require an old parser.

Stop dispatch first. Run the previous version until all prepared/running workers,
spool ingestion and pending publications have drained. Verify actual worker
processes stopped, then run doctor on the new version. If drain is impossible,
stop processes and reconcile side effects before using the existing Workbench
Cancel control. After switching, create a replacement task containing the
remaining approved scope; this creates a new attempt and new prompt. Cancellation
cannot be followed by Retry on that canceled work item. This is a manual
re-preparation procedure, not automatic migration. Never dispatch an old prepared
prompt on the new version, clear the DB/spool, or blindly replay published work.
Installation actions and live model launches require their own deployment scope.

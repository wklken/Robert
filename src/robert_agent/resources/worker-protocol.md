# Worker Protocol

The worker receives a task id, attempt id, workstream id, expected output, allowed GitHub actions, accepted event fingerprints, and optional worktree details.

Phases:

```text
prepare -> analyze -> plan -> execute -> verify -> publish -> handoff
```

Each phase records a snapshot. Commands expected to run longer than five minutes must run through the heartbeat wrapper.

The worker runtime is an internal `robert_agent.worker` module, not an installed
skill. The generated prompt supplies the snapshot, heartbeat, and result CLI
paths needed to execute this protocol from a repository worktree.

If the prompt contains a resume context, the worker is continuing the same
task after a previous attempt made local progress. It must inspect the existing
worktree first, reuse valid edits and command evidence, and record the final
structured result under the current attempt id.

The heartbeat wrapper writes an atomic `*.command.json` lifecycle record in the
task artifact directory, even when `--output-dir` points elsewhere. It records
process identity and output paths before execution, and exit status before the
completion snapshot. A busy/locked SQLite write is reported as
`snapshot_status: deferred`; it does not change the actual command exit code.
The supervisor replays deferred terminal snapshots idempotently and includes
the saved command records in recovery context. Missing processes alone do not
prove a command succeeded. Do not rerun a still-live command, and reuse completed
verification only when its evidence matches the current worktree.

When a model stream terminates after reconnect attempts are exhausted, Robert
prepares a continuation of the same task, including its worktree and command
evidence. Consecutive transport failures wait 30, 60, 120, 240, then 300 seconds
before redispatch, with at most five automatic continuations. Exhaustion is
recorded as `failed_transport_retries_exhausted` and requires an operator retry.
Recorded structured results remain on the existing audit/publication path;
transport errors after result recording must not create a second delivery.

The final result records:

- output type
- planned GitHub actions as objects with `type` plus target URLs when applicable
- consumed event fingerprints
- used skills, including every local skill actually used, or an empty list when none were used
- verification evidence
- handoff summary

The worker may use any installed local skill and should treat `recommended_skills` as route guidance. It must not create, modify, or install runtime skills in the worker's configured skill directories, and must not list the internal worker runtime in `used_skills`.

Repository skill files such as `skills/*/SKILL.md` may be created or modified
when required by the authorized repository task. Symlinks into runtime skill
directories remain protected. Editing repository deliverables does not
authorize installation or activation and does not override read-only
classification or review scope.

If execution needs missing input or a scope decision, record `waiting_for_user`
with `operator_question` and an allowed GitHub comment instead of the ordinary
output type. Setting only `recommended_route: waiting-for-user` on an execution
result fails audit and cannot put the task into a waiting state.

The worker must not create or delete worktrees unless the agent explicitly
provided that operation. Before recording GitHub-facing text, it must apply the
agent redaction rules and stay within `allowed_github_actions`.

Each `planned_github_actions` item uses `type` as its action discriminator.

Workers submit semantic actions according to [the result contract](result-contract.md):
`comment` contains `body` and a `target_url` when target selection is ambiguous;
`open_pr` contains `title` and `body`; `push_existing_pr` contains only `type`.
Robert binds repository, paths, branches, remote and idempotency markers from
trusted state. Workers must not supply those fields or hidden markers.
For `new_pr`, plan `push_existing_pr` before `open_pr` so the branch exists
before PR creation. Issue-sourced `open_pr` also requires `issue_resolution`
(`complete` or `partial`); partial delivery requires nonempty `remaining_scope`.
Robert adds the issue closing/reference statement from that decision.

## Native voting

The `vote` route uses the internal coordinator, not a runtime skill. Its workers
exchange prepared-request.json, ballot.json and synthesis.json via isolated,
sealed file handoffs. A normal final `multi_agent_vote_report` proposes exactly
one audited `comment`, bound to its round, snapshot and triggering Issue/PR.
Missing decision input uses the existing `waiting_for_user` protocol. See
`docs/features/multi-agent-vote.md` for equal single-choice tally and recovery semantics.

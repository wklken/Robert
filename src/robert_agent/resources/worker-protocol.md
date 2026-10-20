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

Use the latest action JSON shape only: each `planned_github_actions` item must include `type`, and workers must not emit the legacy `action` key.

Workers must include the fields required by the publisher for each planned action:
`comment` needs `target_url` and `body`, `open_pr` needs `repo`, `head`, `base`,
`title`, and `body`, and `push_existing_pr` needs `worktree_path` and `branch`.
For `new_pr`, record both `push_existing_pr` and `open_pr` so the branch exists
on the remote before the publisher calls `gh pr create`.
Comment bodies must include the `robert-comment` hidden idempotency marker. New PR
bodies must include the `robert-workstream` hidden metadata block so retries and
follow-up discovery can match the existing GitHub side effect instead of
creating another one.
Issue-sourced `open_pr` actions must set `issue_resolution` to `complete` or
`partial`. Complete delivery must use a GitHub closing keyword for the source
issue. Partial delivery must use `Refs #N` and include a non-empty
`remaining_scope` describing what still needs a follow-up.

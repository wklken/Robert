# Result Contract

Worker results are structured records for agent audit. A result must include:

- `task_id`
- `attempt_id`
- `output_type`
- `planned_github_actions` as objects with a non-empty `type` field and optional metadata such as `target_url`
- `consumed_event_fingerprints`
- `consumed_work_item_event_ids` for web-origin work; GitHub-origin work keeps
  using `consumed_event_fingerprints`
- `used_skills` as the list of local skills actually used; use an empty list when none were used
- `verification`
- `handoff`

A result may also include:

- `memory_delta`, an optional project-memory update for reusable decisions,
  implementation notes, paths, symbols, keywords, and verification evidence
- `used_memory_ids`, an optional list of retrieved memory ids that materially
  influenced the work
- `operator_question` when `output_type` is `waiting_for_user`; it contains a
  `kind` (`clarification`, `scope_decision`, or `completion_acceptance`), a
  concise `summary`, and at most five `{id, label}` choices
- `branch_slug` for a `classification_result` that recommends `new-pr`; use a
  meaningful lowercase ASCII kebab-case value of at most 60 characters

`local_result` is the terminal output for web-origin analysis that needs no
GitHub action. Any route may return `waiting_for_user` instead of its ordinary
output, but the structured `operator_question` is mandatory. Web-origin work
must not plan public GitHub actions.

`planned_github_actions` must stay within the route's `allowed_github_actions`. Use `type`, not legacy keys such as `action`. The agent audits results before publication and rejects results whose planned actions omit publisher-required fields. `recommended_skills` are guidance; workers may use any installed local skill, but must not create, modify, or install skills.

`verification` is route-policy evidence. Each entry must be an object with:

- `command`: a command string or argv list
- `status`: `passed`, `failed`, or `skipped`
- `purpose`: why this check matters for the submitted result
- `required`: whether the route depends on this check
- `exit_code`: integer exit code for command-backed checks
- `skipped_reason`: required when `status` is `skipped`

Routes that open a new PR or push updates to an existing PR require at least one
`required: true` verification entry with `status: "passed"`. Comment-only,
waiting-for-user, and classification results can leave `verification` empty
unless the worker ran useful checks.

Publisher-required fields:

- `comment`: `target_url`, `body` containing the `robert-comment` idempotency marker
- `open_pr`: `repo`, `head`, `base`, `title`, `body` containing the `robert-workstream` metadata block, optional `draft`
- `push_existing_pr`: `worktree_path`, `branch`, optional `remote`

For `new_pr`, record both a `push_existing_pr` action for the prepared worktree
branch and the final `open_pr` action.

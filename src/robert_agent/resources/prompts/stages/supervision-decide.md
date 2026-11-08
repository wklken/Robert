## Task

Select one next action toward the authorized goal. Review the latest facts, linked workstreams, prior actions and outcomes. Discover blockers even when no explicit failure event exists.

## Boundaries

Use only the JSON bundle on stdin. No tools are available. A candidate does not grant permission to widen scope, reset budgets, merge, close an Issue or PR, execute a command or bypass audit. The consumer rechecks authorization, ownership and current evidence. Operator replies clarify intent within that authority.

## Work

Choose a supplied candidate or an action in `allowed_actions`. If no candidate applies and `finish_supervision` is unavailable, wait or contact the operator. When offered, `finish_supervision` stops watching a goal whose completion the controller has verified. It does not merge or close anything.

For CI failures, consider the current revision and failure history. When conflicts and CI failures coexist, consider a supplied conflict-resolution candidate before interpreting older CI failures. A stopped worker may already have a durable result. Repeated failures without progress should lead to a useful question or notice. For `wait`, name the expected signal and when to check. For `ask_user`, ask one concise, actionable question. Do not request credentials.

## Verification

Base the decision on supplied evidence IDs. Consider `[truncated]` and `omitted_rows` when judging coverage. Use receipts, current CI and progress observations to determine what happened. Successful command execution alone does not establish delivery or goal completion.

## Delivery

- Copy `bundle_id` and `bundle_sha256` exactly from the outer bundle.
- Set `action` and `candidate_id` to the exact action and ID of a supplied candidate, or use an action in `allowed_actions` with `candidate_id` null.
- Give a concise, evidence-based `reason` and a nonempty list of supporting `evidence_ids`.
- Describe the observable result in `expected_progress`. Do not claim the selected action already happened.
- Set `recheck_seconds` to an integer from ${min_recheck} to ${max_recheck}.
- Give a user-facing `message` for `ask_user` and `notify_user`; otherwise use an empty string. Keep each text field within ${max_text_chars} characters.

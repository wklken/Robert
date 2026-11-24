## Task

Read the implementation handoff and frozen Review report for this candidate. Recheck findings against the repository and the original request. Decide which findings require changes within the authorized scope.

## Work

For every finding return one disposition: fixed, dismissed, deferred, or needs_decision. Explain the concrete change or evidence, including scope, remaining risk or a decision needed. A reviewer recommendation is evidence to evaluate and grants no new authority. Preserve unrelated changes. Commit fixes locally and leave the worktree clean. Never push, open a PR, or publish comments.

Read the pinned original-request for the complete ticket and discussion before raising an acceptance question. Routine choices already answered by that evidence do not need another decision. When final-feedback is supplied, preserve the previous repair and address only the final review's blockers within the original scope. Verify the registered feedback digests. This is the single bounded targeted repair after the primary repair. Keep candidate_sha bound to the original candidate, even when the assigned worktree starts at the previous repaired SHA. A deferred item must explain why it is nonblocking; unresolved real blockers require fixes or needs_decision.

Verify the final commit with the checks required by the repository. Return its full Git SHA and the verification entries for that commit. Supply an updated PR body with the change summary, verification, assumptions and decisions. Robert adds the Review and dispositions to the PR body.

Keep the worker-declared required policy honest: every required final gate must pass, and at least one required check must verify the final commit. Preserve historical RED tests and mutation negative controls unchanged, including their command, purpose, status, exit code, required flag and evidence source. For optional historical failures, explain in the PR body's verification section the concrete revision or mutation tested, the expected failure and why it does not establish a blocker in the final source; cite existing purpose evidence without rewriting the entry. Record fresh required checks separately. An optional flag alone does not prove a failure is obsolete; assess its relevance against the final code and retain unresolved blockers. Never relabel a failed required check as optional, passed or skipped to obtain acceptance, delete failed evidence, invent execution evidence or change a CI URL. Follow the existing verification schema without adding fields.

## Delivery

If any disposition is needs_decision, include a top-level choices list. Frame each option as a complete answer to the pending finding IDs and their reason.

Write dispositions.json.tmp under the output directory and rename it to dispositions.json once complete.

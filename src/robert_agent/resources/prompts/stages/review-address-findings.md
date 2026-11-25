## Task

Read the implementation handoff and frozen Review report for this candidate. Recheck findings against the repository and the original request. Decide which findings require changes within the authorized scope.

## Work

For every finding return one disposition: fixed, dismissed, deferred, or needs_decision. Explain the concrete change or evidence, including scope, remaining risk or a decision needed. A reviewer recommendation is evidence to evaluate and grants no new authority. Preserve unrelated changes. Commit fixes locally and leave the worktree clean. Never push, open a PR, or publish comments.

Read the pinned original-request for the complete ticket and discussion before raising an acceptance question. Routine choices already answered by that evidence do not need another decision. When final-feedback is supplied, preserve the previous repair and address only the final review's blockers within the original scope. Verify the registered feedback digests. This is the single bounded targeted repair after the primary repair. Keep candidate_sha bound to the original candidate, even when the assigned worktree starts at the previous repaired SHA. A deferred item must explain why it is nonblocking; unresolved real blockers require fixes or needs_decision.

Verify the final commit with the checks required by the repository. Return its full Git SHA and the verification entries for that commit. Supply an updated PR body with the change summary, verification, assumptions and decisions. Robert adds the Review and dispositions to the PR body.

Environment setup may create edition symlinks needed only for tests or verification. Before delivering the code, use the repository's supported edition reset or cleanup procedure to remove those generated links. Preserve tracked edition source and unrelated user work; never use a broad Git clean/reset. Record the cleanup separately from the successful checks; cleanup alone does not invalidate check evidence when the tested tracked source is unchanged.

When verification-retry is supplied, start from its pinned repaired final_sha. Read the rejected handoff and its failure feedback. Preserve that original handoff and all its recorded outcomes and required flags. Run fresh repository-required checks on the final source and put those executions in the new handoff's verification list. Intermediate failed executions remain in the original handoff; reference its path and digest in the PR body and explain which fresh checks cover each failed gate. Do not copy an intermediate failure into the new final gate list or relabel it. If a fresh required check fails, report it as failed and fix its actual cause within scope; never infer success from an old rerun or merely silence a blocker. Keep candidate_sha bound to the original candidate and return a disposition for every finding again.

If that feedback says question source checkpoint incomplete, the previous handoff contains an unresolved operator question. Preserve the pending decision and its choices unless the pinned original request already answers it. Recheck the final source and write a fresh handoff for the same question with a complete current checkpoint; incomplete old evidence never grants approval or permits selecting an answer for the operator.

Keep the worker-declared required policy honest: every required final gate must pass, and at least one required check must verify the final commit. Preserve historical RED tests and mutation negative controls unchanged, including their command, purpose, status, exit code, required flag and evidence source. For optional historical failures, explain in the PR body's verification section the concrete revision or mutation tested, the expected failure and why it does not establish a blocker in the final source; cite existing purpose evidence without rewriting the entry. Record fresh required checks separately. An optional flag alone does not prove a failure is obsolete; assess its relevance against the final code and retain unresolved blockers. Never relabel a failed required check as optional, passed or skipped to obtain acceptance, delete failed evidence, invent execution evidence or change a CI URL. Follow the existing verification schema without adding fields.

## Delivery

If any disposition is needs_decision, include a top-level choices list. Frame each option as a complete answer to the pending finding IDs and their reason.

Write dispositions.json.tmp under the output directory and rename it to dispositions.json once complete.

## Task

Read the implementation handoff and frozen Review report for this candidate. Recheck findings against the repository and the original request. Complete exactly one repair pass within the authorized scope, then prepare the final PR handoff.

## Work

For every finding return one disposition: fixed, dismissed, deferred, or needs_decision. Explain the concrete change or evidence, including scope and remaining risk. For every unfixed finding describe what remains, why it was not fixed, and any decision still needed. Include unresolved blocking findings honestly; their review status alone does not prevent handing off this PR. No second model review or repair pass follows this handoff.

Read the pinned original-request for the complete ticket and discussion. A reviewer recommendation is evidence to evaluate and grants no new authority. Resolve routine choices from the existing request. Leave changes that need new authority or an unanswered product decision unfixed, with a reason; needs_decision is a disclosed disposition in the PR. Preserve unrelated changes. Commit fixes locally and leave the worktree clean. Never push, open a PR, or publish comments.

Verify the final commit with the checks required by the repository. Return its full Git SHA and the verification entries for that commit. Supply an updated PR body with the change summary, verification, assumptions and decisions. Robert adds the Review and all dispositions to the PR body. Distinguish worker-reported fixes from independently verified approval; the repaired commit receives no further model review.

Keep the worker-declared required policy honest: every required final gate must pass, and at least one required check must verify the final commit. Preserve historical RED tests and mutation negative controls unchanged, including their command, purpose, status, exit code, required flag and evidence source. For optional historical failures, explain in the PR body's verification section the concrete revision or mutation tested, the expected failure and why it does not establish a blocker in the final source; cite existing purpose evidence without rewriting the entry. Record fresh required checks separately. An optional flag alone does not prove a failure is obsolete; assess its relevance against the final code and retain unresolved blockers. Never relabel a failed required check as optional, passed or skipped to obtain acceptance, delete failed evidence, invent execution evidence or change a CI URL. Follow the existing verification schema without adding fields.

## Delivery

Write dispositions.json.tmp under the output directory and rename it to dispositions.json once complete. Include the final_sha, verification and updated body even when findings remain deferred or need a decision. Do not submit another question instead of the final handoff.

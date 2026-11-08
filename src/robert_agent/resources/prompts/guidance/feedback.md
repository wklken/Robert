## feedback-classify
PR comment workflow:
First identify what the current request asks; do not classify from historical context alone.
- Ordinary change/question: set review_input_kind="request". Address the request directly within this route; omit review_point_evaluation. Do not invent review points. No review-point workflow is needed for this branch; explicit required_skills still apply.
- Supplied review findings, a review report (including an all-clear report), or a request to act on such a report: set review_input_kind="review_feedback" and follow the review-point steps below.
- If the request includes both review feedback and a separate change/question, set review_input_kind="mixed". Evaluate the supplied findings and handle the separate request; identify the separate request in handoff.
Review-point steps apply to review_feedback and mixed:

## feedback-required
Review-point evaluation (required for this task):

## feedback-common
- Use fast-verify-review-point. Apply adversarial review: check each claim against actual code/tests, counterexamples and counterevidence; keep unsupported claims unverified.
- Return a nonempty review_point_evaluation list. Each entry needs summary, verdict, reasoning and action. verdict: correct, partially_correct, incorrect or unverified.
- Every review report requires a public comment response covering the points and reasons. An all-clear report still needs an evaluation entry and a comment acknowledging no change.

## feedback-comment
- This route is comment-only. action: skip, comment or clarify. Do not modify source or plan a push.

## feedback-update
- action: implement, skip, comment or clarify. Use implement only for correct/partially_correct findings that require changes.
- In a required review workflow or for review_feedback, push only when at least one finding has action=implement. Otherwise comment without pushing.
- After a push, include a comment explaining the changes.

## feedback-mixed
- For mixed, separately requested changes may justify a push even when all review claims are rejected; explain those changes in handoff. Never implement an incorrect or unverified claim.

## feedback-policy
The policy below applies only when evaluating review feedback, not an ordinary request. Put any decision request and its decision-owner mention in the planned comment body.
${review_policy}

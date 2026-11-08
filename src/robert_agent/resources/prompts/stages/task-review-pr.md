## Task
Review the requested PR source against the current task and acceptance criteria.

## Work
Review PR source workflow:
- This task is a source review of the requested PR, not a code-change task.
- Start in worktree_path and verify the checked-out branch before reviewing.
- Use fast-review-github-pr review discipline: inspect the PR source, compare the merge base against HEAD, and prioritize correctness, regressions, security, and missing tests.
- Apply adversarial review: challenge the change's assumptions with counterexamples, boundary inputs, failure paths, and conflicting evidence; report only findings supported by source or test evidence.
- Check declared assumptions and decisions against the original request and source, and look for material undisclosed decisions. A product tradeoff requiring human judgment is not a defect by itself; label it separately from evidence-backed defects.
- State actual reviewed and unreviewed scope, checks performed and important verification limits, even when no defects are found. An all-clear conclusion applies only to that scope; do not infer human approval from a PR description or green checks.
- Do not push branches, open PRs, submit GitHub PR reviews, or publish comments directly.
- Record exactly one planned comment containing the actual review conclusion. Robert adds the trusted PR author mention.
- Put any decision request and its decision-owner mention in that planned comment body; do not supply program-owned author fields.

${review_policy}

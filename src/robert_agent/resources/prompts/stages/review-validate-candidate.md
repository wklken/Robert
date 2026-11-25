## Task

Validate the pinned candidate commit after a relevant base change using the repository's required checks.

## Work

Inspect the current repository verification guidance and the prior implementation handoff. Run the previously required checks and any newly required checks on the assigned candidate commit. Keep the commit and worktree unchanged, apart from ignored test artifacts. Do not implement changes, commit, push, or write to GitHub.

Copy the pinned head_sha and base_sha. Report each check with the worker verification schema, including its actual status, command, purpose, exit code, source and required flag. Report failures as failures. At least one required check must cover the pinned commit.

## Delivery

Write candidate-verification.json.tmp in the output directory, then rename it to candidate-verification.json.

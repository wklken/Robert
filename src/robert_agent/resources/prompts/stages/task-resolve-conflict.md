## Task
Resolve the merge conflict for the pinned PR head and base.

## Work
Merge-conflict remediation workflow:
- The worktree starts at the exact observed PR head.
- Merge the exact observed base with `git merge --no-edit ${base_ref}` and resolve only that merge.
- Resolve every unmerged path, preserve both sides' intended behavior, verify the resolved result, and commit the merge.
- For safety, never rebase and never force-push. Robert will revalidate the remote PR snapshot before publication.

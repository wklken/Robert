# Workstream Model

`source_key` identifies one GitHub issue or PR. `workstream_id` identifies the mainline thread being processed for that source.

Example:

```text
source_key(issue): github:example/backend#123
workstream_id(issue): github:example/backend#123
source_key(pr): github:example/backend!456
workstream_id(pr): github:example/backend!456
origin_workstream_id(pr): github:example/backend#123
```

Issue events stay on the issue mainline. PR events stay on the PR mainline. A DD-created PR carries an origin link back to the issue workstream, but it does not share the issue workstream mutex.

At most one active task can run for a workstream. New events received during an active task become pending events. Workers declare the event fingerprints they consumed; unconsumed events create a child task after the current task finishes.

PR self-healing adds a durable episode keyed by PR workstream, episode kind,
and exact `(head_sha, base_sha)` snapshot. CI failures for configured checks are
aggregated into one `ci` episode; merge conflicts use a `merge_conflict`
episode and are scheduled first. Episode states are:

```text
open -> remediating -> waiting_for_signal -> resolved
open/remediating/waiting_for_signal -> exhausted | superseded | canceled
```

A changed head/base supersedes old work. PR merge or close cancels remaining
episodes. Green configured checks resolve CI episodes without starting a
worker. Budget exhaustion, repeated failure signatures, unavailable CI
evidence, and unsafe local worktree state stop automation and surface operator
attention.

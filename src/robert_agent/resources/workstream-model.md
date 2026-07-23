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

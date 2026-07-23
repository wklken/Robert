# Failure Modes

Business lifecycle and technical status are separate.

Task lifecycle:

```text
detected -> authorized -> classified -> queued -> running -> completed
detected -> ignored
running -> waiting_for_user
running -> failed
running -> canceled
```

Technical details live in attempt, lease, notification, authorization, and failure fields.

Default timeouts:

- stale after 20 minutes: warn locally, do not immediately kill
- hard timeout after 90 minutes: terminate the worker attempt
- if the stored worker process has already exited and recovery inspection finds
  recoverable progress, prepare a same-task resume attempt even when the
  attempt has crossed the hard-timeout threshold
- failed-timeout attempts from earlier runs may be resumed by the supervisor
  when they are still the latest attempt for the task and the same dead-process
  plus recoverable-progress checks pass
- implementation retry default: zero retries unless the route explicitly allows one
- if the worker process exits but a heartbeat-wrapped command is still writing
  running snapshots, keep the attempt running with `orphaned_command_running`
  metadata instead of starting a duplicate worker
- if the worker process exits after recoverable progress, such as dirty
  worktree state, command completion evidence, or worker log tails, mark the
  old attempt failed and prepare a same-task resume attempt with a recovery
  context artifact

Local environment, permission, script, and network failures normally notify locally instead of posting GitHub comments. Public comments are allowed only after redaction.

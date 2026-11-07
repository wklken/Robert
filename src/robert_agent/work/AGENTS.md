# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Keep inputs, authorization policy, workstream identity and state queries in their
  owning modules. Read models should use `control_queries.py` and
  `operator_queries.py` for facts.
- Put admission, command handling, preparation, source lifecycle and completion
  under `application/`. Derive task and attempt authority from the stored
  workstream, accepted inputs and repository scope. An observed GitHub event
  does not by itself authorize a new task.
- Preserve repository and work-item scope, expected version/event checks and
  idempotency for operator commands. Commit durable state and wakeups before
  notifying another process. Recheck active ownership before changing execution.
- Test validation and policy with small inputs. Use a temporary database to
  verify command replay, stale versions, denied scope and durable handoff.

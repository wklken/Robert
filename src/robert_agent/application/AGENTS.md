# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Compose repository-cycle use cases. Keep admission, dispatch, audit,
  publication and completion rules in their respective domains.
- Preserve repository lease ownership, savepoint and durable checkpoint
  behavior, and explicit collaborator wiring. Supply the work preparation
  capability to execution recovery through `resume_attempt`.
- Keep dependency direction from composition to use cases. Scheduling stays in
  `runtime/`, and owning domains must be usable without this package.
- Cover cross-domain ordering, rollback and recovery in repository-cycle
  integration tests.

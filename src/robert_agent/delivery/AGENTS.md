# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Keep submission shape and pure policy in `contract.py` and `policies.py`.
  Stable modules provide locks, results, intents and query capabilities.
  Validation, audit, publication and reconciliation belong under `application/`.
- Treat submissions as proposals. Resolve trusted action scope from durable task
  and attempt bindings, validate and audit the actions, then recheck current
  scope and ownership before effects. Worker CLI code delegates to
  `application/submission.py`.
- Preserve per-task publication locks, durable intent keys and receipts. If an
  external effect may have started, reconcile against the remote before retrying.
  Keep dry-run free of publication writes and expose only redacted diagnostics.
- Test contract rejection, spool replay and stale scope. Exercise duplicate
  publication, ambiguous effects and receipt recovery with controlled responses.

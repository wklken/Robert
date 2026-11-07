# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Keep rule evaluation in `rules.py`, observed evidence in
  `application/observation.py`, and durable incident and recovery state in
  `store.py` and `actions.py`. Diagnostic recommendations stay separate from
  the authority to apply an action.
- Distinguish liveness, progress, missing evidence and proven failure. Bound
  observation and redact evidence before recording or exposing it. Preserve
  stable lineage and incident identities across retries.
- Revalidate observations under the writer. Fence recovery with the current
  policy, claim and generation, and preserve action budgets. Verify recovery
  using positive progress or matching durable receipts as appropriate.
- Test rule classification with controlled facts and time. Verify stale
  observations, fenced claims and receipt mismatches against persisted state.

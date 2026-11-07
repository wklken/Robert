# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../AGENTS.md) here.

- Keep review, vote, PR remediation and maintenance policy in their workflow
  modules. Use shared execution stages and the matching SQLite round store for
  process ownership, slot accounting and cleanup.
- Preserve each round's source snapshot, task and attempt binding, and revision
  identity. Recheck source freshness before reusing artifacts or advancing a
  round. Coordinators submit results through the delivery use case. Publication
  helpers run within the audited publication path.
- Preserve remediation episode budgets and maintenance authorization, including
  restricted launcher proof and the original incident's verification criteria.
  Route selection consumes the fixed policy from `configuration/routes.py`.
- Test workflow-specific stage transitions, stale sources, author selection and
  partial retries at the owning workflow. Cover execution mechanics with the
  shared stage tests.

# Remediation execution record

Base: `764c5e3771565899b4b7dcf45044a229a31fb591`; tracked tree clean; prior review bundle untracked and preserved. Source report and finding verdicts are immutable. Local changes only; no commit/push/publication.

## Contracts and scope

- Fix verified runtime defects R01–R14, conditional R20, and T01. Apply only verified subclaims.
- Safe optimizations R15–R18/R21–R22: reduce duplicate work with identical output, ownership and audit guarantees. Never remove source-integrity verification.
- R19 evidence deletion/retention policy is awaiting the user's preference. R23–R26 preserve stated policies; N01 rejected; U01 requires a normal-path reproducer.
- Result receipt must not release live process ownership/capacity or let a conflicting successor execute. Preserve result idempotency and output schema.
- External process/Git/GitHub I/O must run outside the SQLite writer; revalidate current state after observation. Unknown identities remain attention.
- All lifecycle facts, handoffs, retry deadlines and dedupe evidence survive restarts.

## Ordered units and ownership

1. Parent: lifecycle regressions and run_once/worker.result corrections (R01/R02/R03/R05/R11), then lightweight scheduling predicates and recovery.
2. One implementation-worker, exclusive files: worker/heartbeat.py, worker/snapshot.py, model_execution.py, review_files.py, review_store.py, vote_store.py, resources/db/schema.sql, tests/test_runtime_remediation.py, tests/test_vote_recovery.py. R04/R07/R15/R16/R17/R18/T01. Parent does not edit those files until handoff.
3. Parent: daemon/loop_engine budgets, lightweight supervision, quota gates and retention catch-up (R06/R08/R09/R10/R12/R14); discover intent (R13); spool scan fairness (R20); initialization gate (R21), compatible diagnostics (R22).
4. Integrate, inspect actual diff, inventory changed symbols and dynamic entrypoints, run focused tests, full Python suite, CI lint/type scopes, compile/build (schema resource changed), diff whitespace. Independent read-only final review after implementation-worker exits.

## Verification and rollback

For each feasible bug regression: add test, observe intended failure, fix, rerun. Store command evidence and content identity in the working remediation ledger. Reuse fresh unaffected results; no broad reruns merely at phase boundaries.

Use existing `.venv` via `source .venv/bin/activate`; supported gate `python3 -B -m unittest discover -s tests`. All DB/process tests use temporary fixtures. No production migration or cleanup is performed. No runtime dependency changes planned. New metadata must be backward compatible; old rows without it use conservative process observation.

If a resource cannot be verified stopped, retain ownership and expose attention. Do not unlink stable lock inodes online or delete pending/audit facts. Proposed performance changes must preserve outputs and compare representative synthetic workloads before/after where useful.

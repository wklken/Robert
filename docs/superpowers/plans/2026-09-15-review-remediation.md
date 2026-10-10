# Review lifecycle remediation implementation plan

**Goal:** Repair confirmed findings R1–R8 from the review of main at `214fc275365705ef49a60d55d6367201a2dbced3`, preserving existing trust, audit, workstream and idempotency contracts.

**Architecture:** Keep existing module boundaries. Make workspace reuse safe, process termination complete, terminal states durable, route policy consistent across task creation, and publication recoverable against a fixed result revision.

**Stack:** Python >=3.10, SQLite, Git; no new runtime dependency.

## Scope and execution

Original checkout contains unrelated uncommitted workflow changes. Work only in the isolated `codex/fix-review-lifecycle-boundaries` branch. Use failing behavior regressions before fixes, focused checks after each unit, then the required full suite and CI lint/type checks. User authorized one PR. Implementation is local; independent final review is read-only.

| ID | Confirmed defect | Target modules / verification | Status |
| --- | --- | --- | --- |
| R1 | Review reset discards unmanaged/dirty workspace edits | worktree; temporary Git regressions | fixed |
| R2 | Timeout leaves live process descendants after task release | run_once/dispatch; process termination regressions | fixed |
| R3 | Repeated failed repair resets exhaustion on next poll | remediation; repeated CI/conflict snapshot regressions | fixed |
| R4 | Successful push followed by crash cannot reconcile | publish/run_once; fixed SHA and crash/retry regressions | fixed |
| R5 | Child routes omit configured required skills | run_once; config-to-child integration regressions | fixed |
| R6 | PR head and dedupe omit fork identity | audit_result/publish; mismatched owner regressions | fixed |
| R7 | Reused analysis workspace stays stale | worktree; upstream advance regression | fixed |
| R8 | Canceled work items can create hidden tasks | work_items/run_once; terminal command regressions | fixed |

## Checkpoints

- [x] R1/R7: reject unmanaged or dirty workspaces before reset; refresh clean analysis snapshots.
- [x] R8/R3: enforce terminal commands; preserve exhaustion for unchanged failure context across polls.
- [x] R5: resolve configured route and skill gate for every child/local task entry.
- [x] R2: terminate worker process groups and verify exit before releasing tasks.
- [x] R4/R6: bind publication to trusted result SHA and repository identity; reconcile prior effects.
- [x] Inspect combined diff, run `python3 -B -m unittest discover -s tests`, Ruff and CI-scoped Mypy; perform independent review and address confirmed findings.
- [x] Update this ledger and pass `git diff --check`; prepared for authorized commit and PR delivery.

Run commands in the isolated worktree using the existing project venv and `PYTHONPATH=src:.`. Each finding retains its original ID; mark fixed only after its regression and required final checks pass. No production GitHub writes during test execution.

## Verification evidence

Each R1–R8 regression was observed failing before its repair. Additional tests
cover remote-close cleanup retention, blocked-workspace dispatch, the
controller-observed commit/fork, and an already-pushed remediation result.
Independent review added regressions for prepared-task rerouting, missing fork
identity, legacy new-PR result SHA, and missing process cleanup handles.

Final local baseline after repairs: 667 tests passed; Ruff passed; CI-scoped
Mypy passed for 9 source files; source distribution and wheel build passed;
`git diff --check` passed. Existing ResourceWarnings remain visible in the
suite output. Checks used Python 3.14.7 with the existing project venv; no
production worker, GitHub write, or service-manager acceptance was performed.

R4 legacy limit: accepted pushes missing a fixed result SHA cannot safely
infer which local revision was audited. They fail explicitly and require a new
task result; current results persist the controller-observed SHA at audit time.

Independent final review inspected all 16 files and the four follow-up fixes;
no remaining evidence-backed blocker or actionable regression was found.

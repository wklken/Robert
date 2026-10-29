# Lifecycle and idle-work review remediation

Source review: Robert `293d15aadb94985c4ba2dd03037ac4ac1676a1b7`, reviewed on
2026-10-29. All six findings were reproduced against that clean revision and
explicitly selected for repair and PR delivery. This is a repository review,
not attribution to an earlier PR; introduction is not applicable.

The scope is process ownership during failed launch, review/vote output-directory
recovery, explicit cancellation visibility, projection query cost, fractional
poll deadlines, and capacity-aware local scheduling. No dependencies, database
migration, publication policy, or worker-model configuration changes are planned.

| ID | Severity / verdict | Status | Source evidence | Change and verification |
| --- | --- | --- | --- | --- |
| R1 | P1 / Correct | fixed | `dispatch.py` nonzero startup exit leaves descendants alive while capacity becomes zero | Group cleanup checks descendants after TERM and KILL. Unconfirmed cleanup retains a stale attempt, attention and capacity. `LaunchOwnershipTests` covers live descendants, failed cleanup and safe later cancellation; the native coordinator regression checks cleanup before slot release. |
| R2 | P2 / Correct | fixed | `review_store.new_run` and `vote_store.new_run` directory survives transaction rollback | Unique run IDs isolate output directories. `RunDirectoryRecoveryTests` covers rollback and untouched orphan results in all three stages of both stores. |
| R3 | P2 / Correct | fixed | create/start/cancel yields Board history but Workbench needs_attention without available controls | Shared cancellation facts retain unresolved actions, execution attention and derived workers. `WebReviewRemediationTests` exercises normal cancellation and each remaining-work guard. |
| R4 | P2 / Correct | fixed | 1,000 canceled tasks: 20,003 Board SQL statements and 1,000 full controls reads | Shared controls and scoped detail loading remove repeated full-table materialization. Tests retain counts, repository filters, ancestor lifecycle, scoped controls and legacy precedence. |
| R5 | P2 / Correct | fixed | 0.402 seconds of idle countdown yielded 186 decisions and lease commits | Fractional waits are preserved. `test_fractional_deadline_waits_instead_of_spinning` verifies an actual 0.4-second sleep request. |
| R6 | P2 / Correct | fixed | Two full-capacity drains: no_progress, 2 agent_runs, 42 step rows, 2 leases | Prepared-only work observes global/repository capacity. `IdleSchedulingTests` verifies no empty run creation, capacity-independent supervision/manual wakeups and another repository's available slot. |

## Verification

Regression tests were observed failing before implementation: the runtime group
reported six failed assertions and six directory-collision subtest errors; the
Web regressions also failed before their corresponding fixes. The native model
startup test was updated to require group cleanup before releasing capacity,
while other unresolved-group cases continue to require attention.

- Full Python suite: **1,305 tests passed** in 237.366 seconds using Python 3.14.7
  in the existing development virtual environment.
- Final process-test fixture synchronization adjustment: all **9 runtime
  regressions passed** separately; production code was unchanged during full
  verification. The independent reviewer also checked this adjustment.
- Frontend Node unit tests: **29 passed**. These are not browser E2E tests.
- Ruff, CI-scoped Mypy (9 files), compilation, wheel/sdist build and
  `git diff --check`: passed.
- Independent final review: no blocking findings. The reviewed production
  snapshot remained unchanged through verification.

The suite still emits ResourceWarnings, including unclosed connections and a
subprocess warning at shutdown. This is not a zero-warning result. No production
database, live AI worker, or cross-platform version matrix was exercised.

Using the same synthetic fixture with 1,000 canceled work items, tasks and terminal
controls, an empty Board request now executes **3 SQL statements and zero controls
reads**, versus 20,003 statements and 1,000 full controls reads before. Local
measurements were about 1 ms for that Board request and 2 ms for one Workbench
detail. These timings are workload-specific, not production latency guarantees.
Visible candidate cards still require per-item queries; this change removes the
confirmed quadratic controls cost rather than claiming constant cost for all views.

## Refactor audit

| Symbol / boundary | Audit result |
| --- | --- |
| `run_once._live_process_group` | Implementation moved to `worker_identity.live_process_group`; the import alias preserves daemon consumers and existing test seams without a forwarding wrapper. Production, test and historical documentation references checked. |
| `read_controls`, `lifecycle_facts`, Board projection helpers | Optional scoped/shared inputs retain existing callers. All current call sites checked; subject queries use the composite primary-key index. |
| Workbench `_load_state` / `_group_rows` | Detail loading is restricted to the target and ancestors; grouped ordering, shared lifecycle and list behavior retained by regression tests. |
| Review/vote run directories | Consumers read persisted output paths; no production parser depends on the old `attempt-N` basename. Orphan outputs remain isolated, not automatically deleted. |

No new dependencies, migrations, network calls or GitHub action types were added.
Failed launch cleanup performs bounded process-group signals and `ps` inspection
within the existing launch ownership boundary. A later unverified PID is never
signaled; operators can cancel after the group is proven stopped.

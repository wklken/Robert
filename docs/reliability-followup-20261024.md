# Reliability remediation and isolated acceptance

Scope: the historical A–F, OBS-1, STORAGE-1 and DBOS-1 findings, checked against
`f6c00516963d398de9b67f912aff4b8eb899e1b7` and this follow-up. This is isolated
acceptance, not a production deployment or a claim that historical latency has
been reproduced in today's deployment.

## Closure ledger

| Finding | Implemented contract | Evidence |
| --- | --- | --- |
| A: repeated authentication failures | 401/permission/configuration failures stop; ordinary error logs do not authorize recovery. Only saved work or a structured terminal transport interruption can consume the existing one-shot binding recovery budget. | Recovery budget, transport lifecycle and completion regression tests. |
| C: repeated publication failure | Persisted three-attempt/ten-minute budget; distinguish permission 403 from rate limiting; honor Retry-After within the original budget. Attention/backoff filtering happens before LIMIT. | 1,000 scheduling cycles retain one permanent-failure push; 26 stopped actions do not starve a later healthy result. |
| D: result lost during SQLite lock | Atomic local result spool; replay revalidates task/attempt/owner; stable fingerprint prevents duplicate results/actions. Discovery now commits its phase record before external I/O. | Real 30-second write lock, offline result replay, process death after result commit, canceled replay, and concurrent-writer discovery regression. |
| E: invisible stopped work | Stopped causes and required actions remain visible; notification command has persisted delivery attempts, sent/exhausted states and stable delivery IDs. Task detail now displays delivery state; database health includes exhausted notifications and oldest pending notification/outbox age. | Real isolated receiver subprocess; notification budget and workbench tests. |
| F: undeliverable question | GitHub questions require an operator question and public comment action before result acceptance. Missing delivery action records a corrective operator notice and a visible cleanup state. The supervisor retains capacity and ownership until identity-safe Worker termination, then fails/finalizes the task; a new operator retry can follow. | Question rejection regression; existing audit/redaction contracts remain enforced. |
| DBOS-1: disconnected production entry | Daemon configures production v2 driver before launch; active creation binds before legacy dispatch; restart uses persistent DBOS identity and reconciles publication intent. Existing workflows are not restarted each poll. | Real resident daemon + production driver + DBOS system DB + local bare Git remote: crash immediately after push, restart after lease expiry, exactly one push, task terminal, notification sent. |
| B / OBS-1: unexplained waiting | Actual phase wall times and monotonic durations; usage supports turn.completed. Result spool preserves first saved time and ingestion time, audit records completion. Read-only task timing and grouped P50/P95 appear in status/diagnostic export; detail shows timing, known wait state, next retry and lease expiry. | Clock-controlled 60-second phase test; distinct queue/execution/ingestion regression; real lock measurement and historical grouped results below. |
| STORAGE-1: diagnostic growth | Existing bounded retention preserved; measured cleanup and compaction on writable copies only. No age-based deletion of live ownership or orphan business records. | Copy measurements below, unchanged business counts and integrity results. |

## Interpretation of timings

`robert status` and `diagnostics export` include `execution_timings`, grouping the
latest 500 tasks by `task_kind:route_id:lifecycle`. Each metric reports samples,
missing count, P50 and P95 (nearest rank). Classification, execution, failure and
human waiting are not pooled. Intervals can overlap and must not be summed into
a purported complete wall-time account. Missing/invalid timestamps are unknown,
not zero. Historical Worker durations use attempt boundaries only when dispatch metadata establishes an actual running Worker;
new results separate saved-result time from database ingestion.

The historical snapshot has 169 tasks, including 73 roots, and 614 attempts.
For completed tasks, the grouped measurements are:

| Route | Tasks | Initial queue P50 / P95 | Worker attempts P50 / P95 |
| --- | ---: | ---: | ---: |
| classification-result | 45 | 63.525 / 133.827 s | 102.945 / 496.060 s |
| new-pr | 34 | 2.961 / 633.281 s | 837.414 / 5015.754 s |
| update-existing-pr | 45 | 89.913 / 2906.711 s | 751.241 / 1612.175 s |

The snapshot lacks saved-result/ingestion/audit timestamps, so these metrics
cannot be backfilled reliably. It does not establish whether old queue delays
came from downtime, capacity or leases. No claim of a production P95 reduction
is made. A controlled concurrent-writer comparison does establish the discovery
lock fix: the same probe fails with `database is locked` before the change and
acquires `BEGIN IMMEDIATE` during discovery after it. Serial repository discovery
remains; further parallelization requires a representative deployment workload.

Queue notices start after five minutes, publication notices after two minutes.
These are operational prompts, not latency SLOs. A crashed executor's active
repository lease is preserved until expiry (default nine minutes); detail now
reports this wait and its expiry. No recovery path steals a live lease.

## Storage measurements and maintenance

The source snapshot was opened read-only. Ten batches removed 1,000 eligible
runs in total, each batch of 100 taking approximately 1.2–1.4 seconds on the
local SSD. At the daemon default of one batch per 300 seconds, the upper bound
is 28,800 eligible runs/day; references, active state and retention ages can
reduce this. Use the existing bounded maintenance command for offline catch-up,
inspect each result, and keep a backup. Do not run an unlimited cleanup in a
request path.

After adding current retention indexes and deleting these batches, the copy had
2,422 reusable pages (9,920,512 bytes). File sizes: original 2,047,565,824 bytes;
indexed/pruned copy 2,068,561,920 bytes; `VACUUM INTO` copy 1,977,200,640 bytes.
Index creation can initially grow the file. All copies passed `integrity_check`;
tasks=169, attempts=614, results=160, actions=179, workstreams=84 remained equal.

Two pre-existing `route_decisions` rows (rowid 1 and 3) reference absent tasks.
They were preserved. A targeted maintenance procedure is: export the full two
rows and foreign-key evidence to a sidecar; establish whether an authoritative
backup can restore the referenced tasks; otherwise quarantine only those exact
orphan rows in a copy, recheck all business counts and foreign keys, then review
that copy before any production replacement. Never synthesize a parent task or
cascade-delete business history to make the check pass. Compact only a stopped
or backed-up copy, compare counts and integrity, and retain the original until
replacement acceptance. This change performs no production data maintenance.

## Runtime and delivery boundaries

The restart test runs real daemon and DBOS code in separate processes. It uses
DBOS's SQLite system backend and a local Git remote; only the system database
transport and GitHub discovery are isolated. The production configuration still
requires PostgreSQL. The test explicitly advances expired daemon/repository
leases after confirmed process death. Other tests cover duplicate signals,
result/commit crash gaps, ownership/cancel fences and bounded delivery failures.
These are repeatable failure tests, not proof against every possible crash or
network partition. Deployment-specific PostgreSQL disconnect/failover, actual
Worker commands and real notification channels still require rollout acceptance.

Notification delivery is **at least once**. The receiver must durably deduplicate
`delivery_id` and return success only after accepting it. Sender death after
receiver acceptance can cause a repeat; an exhausted sender exposes failure and
does not silently retry forever. This acceptance sends no production messages.

CI continues testing Python 3.10–3.14 on Linux/macOS. Its dependency audit reads
the actual tested site-packages using an isolated Python 3.14 auditor and audits
that auditor itself. `pip-audit` is no longer installed in the Python 3.10 dev
extra because its file-cache dependency's security fix requires Python 3.11+.
No advisories are ignored and runtime Python 3.10 support is retained.

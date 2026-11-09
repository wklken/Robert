# GitHub polling and local progress

[文档目录](../README.md) · [功能专题](README.md)

The resident daemon runs three independently supervised lanes:

- **Scanner:** reads GitHub through `gh`, commits observations and a wakeup for repositories with events or tracked/pending sources, then continues scanning. Empty scans still refresh read caches without waking local execution. Remote I/O does not hold the repository's task-processing lease.
- **Local drain:** authorizes and routes observations against current local state, supervises workers, audits results, and finalizes published results. Each repository's prepared tasks can dispatch immediately after its transaction commits, within the existing global and repository capacity limits.
- **Publisher:** performs audited GitHub actions and configured operator notification delivery. It retains the existing task publication locks, fresh remote validation, retry policy, and idempotency checks. A slow publish does not block unrelated local dispatch.

The daemon owns scanner and publisher subprocesses. Their startup identity is persisted before they execute remote work. Stop/restart checks process identity and process-group cleanup; unproven cleanup is reported as attention rather than treated as successful termination. Recovery is limited to expired/released prior owners of the same daemon scope.

`robert daemon run --once` and standalone `run once` retain the synchronous diagnostic path. The three lanes apply to the resident service.

The resident local drain checks readiness again before creating run diagnostics
and before entering each repository pipeline. Idle repositories create no step
or lease records. A pending operator notification schedules the publisher
independently; it does not start repository pipelines. Empty event lists still
reach local processing when tracked sources need closure or PR health checks.

## Diagnostic history

Run summaries and business events remain separate from detailed controller steps.
The pipeline step references its completed run's repository summaries
instead of storing the same list twice. Existing full outputs remain readable.

Maintenance keeps full detail for the newest 200 runs, running/failed runs, runs
referenced by wakeups or active leases, and each repository's latest successful
route completion. Older successful runs with every execution, publication and
reconciliation counter explicitly zero retain their summary while step rows and
released repository leases are compacted. Reading events or authorizing inputs
alone does not require retaining every no-action step. Missing counters, malformed
summaries and recorded step failures retain their detail. CLI and Web projections
explain when a run's details were compacted.

Configured run and failure retention periods still govern whole-history removal.
`robert maintenance prune` previews the affected row counts; `--apply` performs
bounded transactions. Maintenance does not automatically vacuum the database.

## Read reuse and polling

| Read | Behavior |
| --- | --- |
| Successful JSON GETs | Deduplicated within one scan, including discovery, source-state reconciliation, and enabled PR health checks. Failures are not cached as successes. |
| Notifications | A first-page conditional GET uses `If-Modified-Since`. HTTP 304 reuses a complete local notification snapshot. HTTP 200 follows pagination; the snapshot and `Last-Modified` marker are saved together only after every page succeeds. Notifications remain unread on GitHub. |
| Search | Successful results are reused for five minutes. Changed notification hints for that repository force a refresh. A search-quota shortage can reuse the last successful snapshot up to thirty minutes old while notifications continue. The five-minute freshness deadline and thirty-minute retention deadline are separate; older snapshots are not replayed. |
| Source discovery | Complete event outputs are cached by source version, notification hints, account/host, trust settings, effective review allowlist, and known-workstream membership. The cache expires after thirty minutes, forcing a full detail refresh. |
| Reconciliation | Sources selected by the existing workstream reconciliation query, plus sources in pending non-attention batches, are checked each scan. Historical unauthorized sources without a workstream do not enter that query. An open PR needs only its issue state; a closed PR uses `pull_request.merged_at`, with a pull-detail fallback when that field is absent. |
| CI and publication validation | Continue their own current-state checks; discovery caches do not authorize publication or replace CI evidence. |

Notification polling follows `X-Poll-Interval` when supplied, with a 60-second fallback after a completed scan. `Retry-After` on notification responses and core-quota reset times can extend the delay. A full worker pool does not increase the resident scanner interval to 600 seconds. `github.polling.fallback_seconds` still controls delayed startup and recovery after a scanner failure without a returned interval. Polling is scheduled after a scan completes, so a slow scan cannot overlap another scan in the same daemon. While a scan is running, the daemon waits on its local polling/wakeup interval instead of treating the elapsed remote poll deadline as immediate work. An unchanged child-attention condition is reported once.

Context changes create a `github_context_refresh` wakeup. An idle resident scanner
can refresh those repositories immediately with cached notification hints, without
polling notifications early or moving their normal deadline. Refresh has its own
5-second success / 30-second failure gate and honors longer core-quota reset delays.
The existing scan-lane retry budget still applies.

## Durable observations and retries

SQLite stores complete discovery events, including discussion text needed for local replay. This is not a `since`-based partial discussion mirror. Keep the database under the same access and backup controls as existing `github_events`; caches do not appear in command metrics or public replies. Existing audit and publication redaction gates remain in force.

A cache hit skips remote reads, **not local authorization or routing**. A failed worktree preparation, busy repository lease, or failed local pipeline retries after 30 seconds with exponential backoff capped at 300 seconds. Five failed attempts retain the batch in attention, record an operator notification, and stop automatic replay. A failed context refresh has the same bound. Context-mismatched batches wait for a fresh scan without waking local drain; publisher-only work wakes only the publisher. New observations do not overwrite unconsumed events. When discovery configuration changes, a fresh scan carries each pending event identity into the new context and local authorization runs again; source-level coverage alone cannot discard an older event. Coverage is tracked per source: an unrelated failed read does not invalidate every source. When one replay source is unavailable, readable events can still route and the run records the partial discovery failure.

The latest successful or failed safety read is stored per endpoint, so a new failure cannot fall back to an old successful state. Reads older than one hour are unavailable for replay. Completed batches and their scan wakeups are removed; pending batches survive until handled. Expired discovery caches are removed during cache writes.

Accepted worker results can leave additional unconsumed requests. Their ordinary
handoff is persisted before worktree preparation and survives a controller restart.
Transient preparation failures use at most three attempts within ten minutes, with
backoff; permanent failures stop immediately. The completed parent result is retained.
An exhausted handoff appears as a work-item execution failure; **Retry** resumes the
pending request without repeating the completed parent work. Failed GitHub **Retrigger**
preparation uses the same budget and exposes a new Retrigger once the cause is repaired.

After a local control lane stops admitting work, the daemon still runs a bounded
supervision child for existing workers. It respects repository leases and task locks,
checks process identity, enforces timeouts and records confirmed exits. It does not
prepare recovery attempts, route events, audit or publish results, or clear the pause.
Native review/vote sessions retain their history, but proven-stopped model groups and
parents can release occupied capacity. Unknown process ownership continues to require
operator inspection.

Unstarted native review/vote rounds are reauthorized against current trusted actors
and route enablement both when materializing a task and before dispatching a prepared
attempt. A saved request is audit evidence, not a permanent execution grant.

Issue-body mentions use the body content as their request identity. Comments or
labels that only change the Issue activity timestamp do not repeat a consumed
body request. Previously recorded body requests retain their identity when the
body and author still match. Mentions inside HTML comments are ignored. Trusted
GitHub login comparisons are case-insensitive throughout admission and replay.
For edited Issue comments, discovery reads the current body and editor from the
same GraphQL node response; unavailable editor evidence cannot inherit the
original author's authority. Older cached enrichment is refreshed. Preserved old
comments without editor evidence remain non-authoritative until a fresh matching
observation is available; unrelated discussion cannot authorize an older task.

Before each new publication write, the publisher rechecks loaded trust and route
policy. Revocation blocks that write and leaves an operator-visible reason.
Unknown prior effects can still be reconciled through reads; confirmed absence
does not permit resending under revoked authority. Comment deduplication requires
the configured publisher and exact body, in addition to its marker. Existing-PR
pushes also verify the actual PR author, head branch and head repository against
the configured push destination; a copied marker cannot establish ownership.

## Inspecting and recovering attention

List batch state without printing event bodies; after resolving the underlying access, configuration, or local failure, explicitly retry a batch:

```bash
python3 -m robert_agent.runtime.scan --config ~/.config/robert/config.yml --list-batches
python3 -m robert_agent.runtime.scan --config ~/.config/robert/config.yml --retry-batch BATCH_ID
```

Retry is limited to repositories in that configuration. It resets the retry budget, preserves the original events, and wakes the daemon. A mismatched context still waits for the next permitted remote scan. A batch returning HTTP 410 is retained for inspection rather than silently discarded or retried forever.

Control children record a Linux boot ID/PID namespace or macOS boot-session UUID. A proven change of boot session on the execution host retires the old record without signaling a potentially reused PID. A namespace change alone, missing boot evidence, or ambiguous same-boot PID reuse does **not** prove that old descendants stopped; daemon startup remains blocked in those cases.

For that fallback, first stop the service and verify that its old scanner/publisher execution context and descendants have stopped. Then inspect the saved records and explicitly acknowledge only the relevant child:

```bash
python3 -m robert_agent.runtime.control --db /path/to/robert.db --list
python3 -m robert_agent.runtime.control --db /path/to/robert.db --acknowledge-stopped --id CHILD_ID
```

Acknowledgment never signals a process. It records the operator assertion and UID in `control_child_retired` audit evidence, refuses a live owner's unexpired daemon lease, and lets a subsequent startup recover. It must not be used merely because a PID looks unfamiliar.

Request/list output and reason statistics label scan wakeups `github_observation_ready` or `github_context_refresh`, separately from human operator requests.

## Metrics

Each `scan_completed` daemon event contains `github_reads`, grouped by account and endpoint: CLI invocation count, total command duration, cache hits, and errors. An invocation using pagination may perform several HTTP requests; these are **CLI counts**, not exact GitHub request counts. HTTP 304 is not counted as a failed read.

Split event-to-publish latency into discovery wait, local queue wait, worker
execution, and publication when diagnosing a slow task. Notification probes,
quota probes, reconciliation and optional CI reads add work beyond discovery.

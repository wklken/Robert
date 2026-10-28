# GitHub polling and local progress

The resident daemon runs three independently supervised lanes:

- **Scanner:** reads GitHub through `gh`, commits observations and a wakeup per repository, then continues scanning. Remote I/O does not hold the repository's task-processing lease.
- **Local drain:** authorizes and routes observations against current local state, supervises workers, audits results, and finalizes published results. Each repository's prepared tasks can dispatch immediately after its transaction commits, within the existing global and repository capacity limits.
- **Publisher:** performs audited GitHub actions and configured operator notification delivery. It retains the existing task publication locks, fresh remote validation, retry policy, and idempotency checks. A slow publish does not block unrelated local dispatch.

The daemon owns scanner and publisher subprocesses. Their startup identity is persisted before they execute remote work. Stop/restart checks process identity and process-group cleanup; unproven cleanup is reported as attention rather than treated as successful termination. Recovery is limited to expired/released prior owners of the same daemon scope.

`daemon --once` and standalone `run once` retain the synchronous diagnostic path. The new lanes apply to the resident service.

## Read reuse and polling

| Read | Behavior |
| --- | --- |
| Successful JSON GETs | Deduplicated within one scan, including discovery, source-state reconciliation, and enabled PR health checks. Failures are not cached as successes. |
| Notifications | A first-page conditional GET uses `If-Modified-Since`. HTTP 304 reuses a complete local notification snapshot. HTTP 200 follows pagination; the snapshot and `Last-Modified` marker are saved together only after every page succeeds. Notifications remain unread on GitHub. |
| Search | Successful results are reused for five minutes. Changed notification hints for that repository force a refresh. A search-quota shortage can reuse cached search results while notifications continue. |
| Source discovery | Complete event outputs are cached by source version, notification hints, account/host, trust settings, effective review allowlist, and known-workstream membership. The cache expires after thirty minutes, forcing a full detail refresh. |
| Reconciliation | Tracked sources are still checked each scan. An open PR needs only its issue state; a closed PR uses `pull_request.merged_at`, with a pull-detail fallback when that field is absent. |
| CI and publication validation | Continue their own current-state checks; discovery caches do not authorize publication or replace CI evidence. |

Notification polling follows `X-Poll-Interval` when supplied, with a 60-second fallback after a completed scan. `Retry-After` on notification responses and core-quota reset times can extend the delay. A full worker pool does not increase the resident scanner interval to 600 seconds. `github_poll_seconds` still controls delayed startup and recovery after a scanner failure without a returned interval. Polling is scheduled after a scan completes, so a slow scan cannot overlap another scan in the same daemon.

## Durable observations and retries

SQLite stores complete discovery events, including discussion text needed for local replay. This is not a `since`-based partial discussion mirror. Keep the database under the same access and backup controls as existing `github_events`; caches do not appear in command metrics or public replies. Existing audit and publication redaction gates remain in force.

A cache hit skips remote reads, **not local authorization or routing**. A failed worktree preparation, busy repository lease, or failed local pipeline keeps the observation pending with a 30-second retry. New observations do not overwrite unconsumed events. When discovery configuration changes, a fresh scan carries each pending event identity into the new context and local authorization runs again; source-level coverage alone cannot discard an older event.

The latest successful or failed safety read is stored per endpoint, so a new failure cannot fall back to an old successful state. Reads older than one hour are unavailable for replay. Completed batches and their scan wakeups are removed; pending batches survive until handled. Expired discovery caches are removed during cache writes. No existing domain tables are migrated destructively.

## Measuring the result

Each `scan_completed` daemon event contains `github_reads`, grouped by account and endpoint: CLI invocation count, total command duration, cache hits, and errors. An invocation using pagination may perform several HTTP requests; these are **CLI counts**, not exact GitHub request counts. HTTP 304 is not counted as a failed read.

The deterministic discovery regression uses one PR discovered through assignment, mention, and notification. On the original code it required 18 CLI calls; the same fixture now requires 8 on a cold cache and 0 detail/search calls on an unchanged warm cache. Notification probes, quota probes, reconciliation, and optional CI reads are additional. This is a synthetic call-count comparison, not a production latency claim.

After deployment, compare matching workloads and split event-to-publish latency into discovery wait, local queue wait, worker execution, and publication. Worker models, effort, prompts, and test policy are unchanged by this optimization.

## Rollback

Stop the service and verify owned children have exited before replacing the binary. The schema additions are compatible with the previous binary. Preserve the database: an older binary ignores the new pending-observation tables and resumes its prior full discovery path, so rollback does not guarantee replay of an event that is no longer discoverable on GitHub. Consume pending observations before rollback when possible.

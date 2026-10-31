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
| Search | Successful results are reused for five minutes. Changed notification hints for that repository force a refresh. A search-quota shortage can reuse the last successful snapshot up to thirty minutes old while notifications continue. The five-minute freshness deadline and thirty-minute retention deadline are separate; older snapshots are not replayed. |
| Source discovery | Complete event outputs are cached by source version, notification hints, account/host, trust settings, effective review allowlist, and known-workstream membership. The cache expires after thirty minutes, forcing a full detail refresh. |
| Reconciliation | Sources selected by the existing workstream reconciliation query, plus sources in pending non-attention batches, are checked each scan. Historical unauthorized sources without a workstream do not enter that query. An open PR needs only its issue state; a closed PR uses `pull_request.merged_at`, with a pull-detail fallback when that field is absent. |
| CI and publication validation | Continue their own current-state checks; discovery caches do not authorize publication or replace CI evidence. |

Notification polling follows `X-Poll-Interval` when supplied, with a 60-second fallback after a completed scan. `Retry-After` on notification responses and core-quota reset times can extend the delay. A full worker pool does not increase the resident scanner interval to 600 seconds. `github_poll_seconds` still controls delayed startup and recovery after a scanner failure without a returned interval. Polling is scheduled after a scan completes, so a slow scan cannot overlap another scan in the same daemon. While a scan is running, the daemon waits on its local polling/wakeup interval instead of treating the elapsed remote poll deadline as immediate work. An unchanged child-attention condition is reported once.

Context changes create a `github_context_refresh` wakeup. An idle resident scanner
can refresh those repositories immediately with cached notification hints, without
polling notifications early or moving their normal deadline. Refresh has its own
5-second success / 30-second failure gate and honors longer core-quota reset delays.
The existing scan-lane retry budget still applies.

## Durable observations and retries

SQLite stores complete discovery events, including discussion text needed for local replay. This is not a `since`-based partial discussion mirror. Keep the database under the same access and backup controls as existing `github_events`; caches do not appear in command metrics or public replies. Existing audit and publication redaction gates remain in force.

A cache hit skips remote reads, **not local authorization or routing**. A failed worktree preparation, busy repository lease, or failed local pipeline retries after 30 seconds with exponential backoff capped at 300 seconds. Five failed attempts retain the batch in attention, record an operator notification, and stop automatic replay. A failed context refresh has the same bound. Context-mismatched batches wait for a fresh scan without waking local drain; publisher-only work wakes only the publisher. New observations do not overwrite unconsumed events. When discovery configuration changes, a fresh scan carries each pending event identity into the new context and local authorization runs again; source-level coverage alone cannot discard an older event. Coverage is tracked per source: an unrelated failed read does not invalidate every source. When one replay source is unavailable, readable events can still route and the run records the partial discovery failure.

The latest successful or failed safety read is stored per endpoint, so a new failure cannot fall back to an old successful state. Reads older than one hour are unavailable for replay. Completed batches and their scan wakeups are removed; pending batches survive until handled. Expired discovery caches are removed during cache writes. No existing domain tables are migrated destructively.

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

## Inspecting and recovering attention

List batch state without printing event bodies; after resolving the underlying access, configuration, or local failure, explicitly retry a batch:

```bash
python3 -m robert_agent.github_scan --config ~/.config/robert/config.yml --list-batches
python3 -m robert_agent.github_scan --config ~/.config/robert/config.yml --retry-batch BATCH_ID
```

Retry is limited to repositories in that configuration. It resets the retry budget, preserves the original events, and wakes the daemon. A mismatched context still waits for the next permitted remote scan. A batch returning HTTP 410 is retained for inspection rather than silently discarded or retried forever. The retry columns are added compatibly when opening a database from an earlier version of this PR.

Control children now record a Linux boot ID/PID namespace or macOS boot-session UUID. A proven change of boot session on the execution host retires the old record without signaling a potentially reused PID. A namespace change alone, missing boot evidence, or ambiguous same-boot PID reuse does **not** prove that old descendants stopped; daemon startup remains blocked in those cases.

For that fallback, first stop the service and verify that its old scanner/publisher execution context and descendants have stopped. Then inspect the saved records and explicitly acknowledge only the relevant child:

```bash
python3 -m robert_agent.control_lane --db /path/to/robert.db --list
python3 -m robert_agent.control_lane --db /path/to/robert.db --acknowledge-stopped --id CHILD_ID
```

Acknowledgment never signals a process. It records the operator assertion and UID in `control_child_retired` audit evidence, refuses a live owner's unexpired daemon lease, and lets a subsequent startup recover. It must not be used merely because a PID looks unfamiliar.

Scan wakeups retain the existing database enum for compatibility, but request/list output and reason statistics label them `github_observation_ready` or `github_context_refresh`, separately from human operator requests.

## Measuring the result

Each `scan_completed` daemon event contains `github_reads`, grouped by account and endpoint: CLI invocation count, total command duration, cache hits, and errors. An invocation using pagination may perform several HTTP requests; these are **CLI counts**, not exact GitHub request counts. HTTP 304 is not counted as a failed read.

The deterministic discovery regression uses one PR discovered through assignment, mention, and notification. On the original code it required 18 CLI calls; the same fixture now requires 8 on a cold cache and 0 detail/search calls on an unchanged warm cache. Notification probes, quota probes, reconciliation, and optional CI reads are additional. This is a synthetic call-count comparison, not a production latency claim.

After deployment, compare matching workloads and split event-to-publish latency into discovery wait, local queue wait, worker execution, and publication. Worker models, effort, prompts, and test policy are unchanged by this optimization.

## Rollback

Stop the service and verify owned children have exited before replacing the binary. The schema additions are compatible with the previous binary. Preserve the database: an older binary ignores the new pending-observation tables and resumes its prior full discovery path, so rollback does not guarantee replay of an event that is no longer discoverable on GitHub. Consume pending observations before rollback when possible.

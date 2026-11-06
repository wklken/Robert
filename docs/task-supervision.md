# Continuous task supervision

Robert's independent supervisor observes the existing control plane. It records
incidents without changing task ownership, cancellation, audit, or publication.
A live heartbeat is activity, not evidence of delivery. An incident only closes
when its rule observes positive progress evidence.

## Run and inspect

After upgrading Robert and initializing its additive database migrations:

```sh
robert supervisor check --config /path/to/config.yml --output json
robert supervisor run --config /path/to/config.yml
robert supervisor status --config /path/to/config.yml --output json
```

`check` is read-only. `run --once` records a single observation cycle. Neither
command creates a missing database or silently migrates an old one. Missing or
busy state is reported as unobservable, not interpreted as a missing task.

```yaml
supervision:
  mode: observe
  poll_seconds: 60
  batch_size: 100
  stall_seconds: 300
```

`off` disables the resident observer. `observe` is the default and never grants
extra business retry authority. `recover_known` is reserved for explicitly
validated, policy-enabled recovery paths. Limits are bounded; tune them against
representative workloads and inspect coverage rather than treating them as SLAs.

## Independent service

```sh
robert service install --target supervisor --config /path/to/config.yml
robert service start --target supervisor
robert service status --target supervisor
robert service stop --target daemon
```

The final command stops the business daemon only. The supervisor remains alive.
`--target all` explicitly controls the complete stack. Service installation does
not imply a service was started or that a real model worker has been verified.

## Evidence and notifications

`supervisor status` and `robert status` expose unresolved incidents. Observations,
incidents, and recovery requests live in additive SQLite tables. No new database
server or runtime dependency is required. Old tasks without reliable provenance
remain observation-only.

Set the existing `ROBERT_NOTIFICATION_COMMAND` in the service environment to
deliver operator notices. The receiver must deduplicate the supplied stable
`delivery_id`; delivery is at least once, not exactly once. Delivery runs outside
the observation loop. Without a configured command, status explicitly reports
`not_configured`; a stored notice does not mean a person received it.

A database outage leaves a bounded, private health record under the data
directory's `supervision/` directory. The service log remains available when that
directory is also unavailable. No business recovery runs from a stale snapshot.
A same-host process cannot detect a complete host power loss; external liveness
monitoring is a separate deployment responsibility.

The design and phased acceptance matrix are in
[the implementation plan](superpowers/plans/2026-11-06-task-supervision-design.md).

## Optional semantic diagnosis

Configure a dedicated administrator-owned wrapper that accepts one JSON evidence
bundle on stdin and returns the structured JSON described in
`resources/supervision-diagnosis.md`. It must expose no model tools. The ordinary
coding adapters are not a diagnosis profile. Example (replace the executable):

```yaml
supervision:
  mode: observe
  diagnosis:
    command: [/opt/robert/bin/no-tool-model]
    environment_allowlist: [MODEL_API_KEY]
    timeout_seconds: 120
    cooldown_seconds: 600
    max_calls_per_hour: 6
```

Only explicitly named model environment variables reach the wrapper. Business
credentials and runtime configuration variables are prohibited. Its private HOME
and working directory reduce accidental configuration reuse; they are not an OS
sandbox. The configured executable is trusted administration code, responsible
for its model API and the no-tool contract.

The supervisor diagnoses unresolved incidents and long-running suspect tasks.
A persisted claim reserves one diagnostic slot and an hourly call before spawn.
An unknown process identity retains the slot for attention instead of spawning
a duplicate after restart. Output must cite the exact bundle/hash and known
evidence identifiers. The bounded, redacted bundle and outcome remain in the
analysis checkpoint. Invalid or stale output cannot authorize business actions.
Model failure does not stop deterministic observations. `check` never calls the
model; the resident `run` loop schedules and reaps diagnosis asynchronously.

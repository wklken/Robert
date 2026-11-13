# Continuous task supervision

[文档目录](../README.md) · [功能专题](README.md)

Robert's independent supervisor observes the existing control plane. It records
incidents without changing task ownership, cancellation, audit, or publication.
A live heartbeat is activity, not evidence of delivery. An incident only closes
when its rule observes positive progress evidence.

## Model decisions for Issue and PR progress

Explicit `mode: decide` gives a dedicated model the choice of the next supported
action for an Issue/PR and its linked workstreams. It observes goals, source
events, tasks, attempts, CI/conflict episodes, execution budgets, progress
observations, results and prior decisions. It also checks work without an
Incident, so a stalled goal can lead to a question or notice. Calls are bounded
and share a persisted installation-wide hourly budget and cooldown.

```yaml
supervision:
  mode: decide
  decision:
    command: [/opt/robert/bin/no-tool-supervisor]
    environment_allowlist: [MODEL_API_KEY]
    timeout_seconds: 120
    cooldown_seconds: 60
    max_calls_per_hour: 60
```

The command is an administrator-provided model API wrapper. It reads one JSON
bundle on stdin and returns the [decision contract](../../src/robert_agent/resources/prompts/stages/supervision-decide.md)
on stdout. It must expose no tools. The private working directory and HOME are
not an OS sandbox. Only explicitly allowed model environment variables reach
the command; ordinary coding adapters are not this profile. An existing
`diagnosis` profile remains advisory and does not implicitly enable decisions.
In `decide` mode the supervisor does not start separate advisory diagnosis calls
or automatic maintenance proposals; the decision loop owns the next action.

The model can select a supplied CI repair, conflict resolution, eligible Worker
retry, or known recovery candidate; wait with a recheck time; notify or ask the
operator. `finish_supervision` is offered only when current domain facts already
confirm completion. It stops observation of that completed goal without closing
an Issue or merging a PR. It does not create arbitrary tasks or run arbitrary
commands. If the goal requires an action outside current capabilities, the model
must ask or notify the operator.

Existing repository `pr_automation` enablement, CI check allowlists and episode
budgets still apply. Known recovery candidates require the existing
`supervision.recovery` repository/action allowlists. Worker retry retains its
existing process-identity, evidence and one-shot recovery budget. In this mode
these automatic consumers wait for a matching model choice; they cannot race
the model's `wait` decision or silently fall back to their old automatic choice.
Normal result ingestion, initial execution, audit/publication validation,
cancellation and required cleanup retain their existing lifecycle owners.

Grants bind the complete sampled context, candidate generation and administrator
policy. Consumers recheck the current context and their own eligibility; new
requirements, results, PR versions or changed policy invalidate stale grants.
An unconsumed grant expires after 15 minutes. Candidate freshness is also bounded
to 15 minutes; scans must refresh candidates before they can be selected again.
An executed action enters verification, and its real result is included in the
next decision. Dispatch and a successful push do not prove progress or green CI.
The model cannot extend budgets by selecting a replacement task.

Questions and notices use the configured messaging transport or notification command and appear in
`supervisor status`. Work-item details show the current decision, reason,
message and next check. Active work items expose supervisor questions through
the existing authenticated Reply flow. A reply updates decision context without
starting a new task or treating the reply as additional execution authority.
For any supervised workstream, a local operator can answer the exact question:

```sh
robert supervisor status --config /path/to/config.yml --output json
robert supervisor reply --config /path/to/config.yml \
  --workstream-id WORKSTREAM_ID --decision-id DECISION_ID \
  --body 'Preserve the existing public API' --output json
```

The identifiers come from the recorded question/status, not a guessed latest
task. A stale question ID is rejected. An unanswered question remains pending;
it is not re-sent each poll. Repeated notices for unchanged evidence are
deduplicated. Delivery requires a configured messaging transport or `ROBERT_NOTIFICATION_COMMAND`;
a recorded question is not proof that a person received it. Work-item questions
retain repository scope and the existing authenticated chat Reply contract.

Malformed model output, timeouts and transport failures cannot grant actions.
The wrapper may report the same bound `rate_limited` transport envelope as the
diagnosis wrapper below; the greater of its Retry-After delay and configured
cooldown applies across targets and restarts. Repeated model failures create an
operator health notice. This is a global service alert in `/operations`' notification
center, rather than a Kanban approval or reply event. Its `ack` action records
receipt only; decisions continue according to the configured cooldown and call
budget. Known failure codes have human-readable explanations. Failed calls retain
the exact decision/workstream identity, time, exit code and bounded, redacted
stderr in supervisor status and the work item's task-supervision details. Oversized,
non-UTF-8 or sensitive stderr is withheld. URLs containing userinfo are treated
as sensitive, including percent-encoded credentials. Raw stderr is never persisted.
Explicit credential assignments are withheld regardless of value length.
Global service alerts contain only service health information, not workstream,
repository or stderr details. This also applies to older alerts waiting for
delivery; inspect the local supervisor status or task details for diagnostics.

After a validated model response and confirmed process stop, the unavailable
alerts become resolved history, preserving their delivery and acknowledgement
records. This establishes that the service can answer again; it does not prove
that every previously failed workstream has retried or completed. An invalid
response, process-start failure or unresolved process does not clear the alert.
An unresolved process identity retains the claim and is
visible as `decision_runner.claim` in status; service restart does not authorize
duplicate launches. When the recorded owner is proven dead and no model identity
was checkpointed, the claim moves to `decision_runner.attention_claims`, keyed by
its root workstream. That workstream remains fenced until operator recovery;
unrelated workstreams can continue decisions under the same shared cooldown and
hourly call budget. A live or unknown owner, or a recorded model identity, keeps
the existing installation-wide process reconciliation fence.

Stop the supervisor service and its model processes, then
release that exact stranded claim through the local operator command:

```sh
robert supervisor resolve-decision --config /path/to/config.yml \
  --decision-id DECISION_ID --confirm-process-stopped --output json
```

The command rejects a known live supervisor/model process or model process group,
fences the unchanged claim and records the operator attestation. Restart the
supervisor after recovery. Unknown identity is never automatically treated as dead.

Targets use a durable bounded scan cursor, so waiting questions do not block
other work. Text/row limits and explicit omission markers keep evidence bounded;
an oversized workstream family is reported for attention without stopping other
targets. Model input contains short summaries of large text, not complete logs.
Observations continue even when decisions are unavailable. `check` remains
read-only and never calls the model.

## Run and inspect

Use an initialized Robert database:

```sh
robert supervisor check --config /path/to/config.yml --output json
robert supervisor run --config /path/to/config.yml
robert supervisor status --config /path/to/config.yml --output json
```

`check` is read-only. `run --once` records a single observation cycle. Neither
command creates a missing database or silently migrates an old one. Missing or
busy state is reported as unobservable, not interpreted as a missing task.
A failed recorded check updates local health immediately. An outage notice
requires at least two consecutive failed observations spanning `poll_seconds`;
a single stale failure cannot trigger a notice later. A successful check resets
that episode. Notification identity and delivery state survive restarts.

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
incidents, and recovery requests live in SQLite. Tasks without reliable provenance
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

The published supervision boundaries are described in the
[architecture guide](../architecture.md#worker-监督与失败恢复).

## Optional semantic diagnosis

Configure a dedicated administrator-owned wrapper that accepts one JSON evidence
bundle on stdin and returns the structured JSON described in
the [diagnosis contract](../../src/robert_agent/resources/prompts/stages/supervision-diagnose.md).
It must expose no model tools. The ordinary coding adapters are not a diagnosis profile. Example (replace the executable):

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
credentials and runtime configuration variables are prohibited. Model wrappers
using Google application default credentials may explicitly allow
`GOOGLE_APPLICATION_CREDENTIALS`; it is not inherited by default. Its private HOME
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
Input limits are measured in UTF-8 bytes. Oversized evidence records a bounded
`evidence_too_large` checkpoint and target-local cooldown without retaining the
payload or charging a model call. The same bounded poll continues to other eligible
targets, including suspect tasks and those in the same lineage; restart preserves
the rejected target's cooldown.

### Model service rate limits

The administrator wrapper may report an actual HTTP 429 with this transport
outcome instead of a semantic diagnosis, on either a zero or nonzero exit:

```json
{
  "bundle_id": "copied from input",
  "bundle_sha256": "copied from input",
  "error": {"code": "rate_limited", "retry_after_seconds": 120}
}
```

Only these fields are allowed. `retry_after_seconds` is optional and must be an
integer from 0 to 86400; the wrapper converts an HTTP-date `Retry-After` into
seconds. Without a usable header, omit the field. This is trusted transport
reporting, not a model-generated interpretation of log text. Do not include raw
HTTP bodies, headers, or credentials. Ordinary diagnosis output still requires
the original six-field schema and a successful exit.

Robert persists `provider_rate_limited` and the next eligible time, then waits
for the greater of `cooldown_seconds` and the reported delay. This cooldown
covers all diagnosis targets in the installation and survives a restart. Each
launch still consumes the hourly call budget; changing incidents cannot bypass
it. Exhausted budgets stop new calls until the rolling hourly window permits
one. A 429 cannot resolve an incident, create a product-defect diagnosis, or
authorize a recovery or repair. Deterministic observation continues without
the diagnostic model.

Business workers retain their adapter's internal retry behavior and Robert's
existing bounded fallback. A heartbeat or retry log is not progress, and does
not grant an additional attempt. There is no shared quota circuit breaker
across business workers: model endpoint and quota ownership remain inside their
adapters. The simulation validates Robert's lifecycle with a bounded fixture
HTTP client, not a particular vendor SDK's backoff implementation.

## Known recovery paths

Known recovery requires `mode: recover_known` or `mode: decide`, plus an explicit repository/action
allowlist. For example:

```yaml
supervision:
  mode: recover_known
  recovery:
    repositories: [example/project]
    allowed_actions: [spool_replay, audit_retry, handoff_resume, publication_reconcile, publication_retry]
```

The existing consumers claim an exact attempt, result, parent task or action.
They retain their original locks, ownership, cancellation checks, audit gates
and retry deadlines. One automatic recovery is allowed per lineage/stage and
three across the lineage. The separate read-only publication reconciliation
stage does not grant permission to repeat a remote effect. Only explicitly
planned supervision requests spend this recovery budget. Native spool replay,
audit retries and publication retries keep their existing domain limits and do
not create unattached recovery charges. Exhausting the supervision budget does
not disable native retries. Outstanding claims and unknown effects still fence
conflicting work, including after recovery is disabled. Scheduling hints do not
reload configuration; actual supervised claims and effects revalidate policy.
Requests and budgets survive restarts and replacement tasks. Unknown lineage
gets no additional automatic authority. The audit Retry command's budget reset
is never used by supervision.

A successful consumer return enters verification. Exact persisted domain
receipts must satisfy the expected progress before the incident closes. Unknown
or overdue effects retain their scope and charged budget, and require attention.
Disabling recovery does not remove those guards. A rejected request for the same
generation is not automatically queued forever. Worker replacement and new
native review rounds continue their existing controls; supervision cannot grant
additional attempts for them.

## Engineering repairs and deployment evidence

A product-defect diagnosis can create one system-owned maintenance WorkItem for
an incident. Configure `repair.repository` to select a repository already in the
Robert configuration. `mode: backlog` is the initial policy: it creates a local
proposal with bounded technical evidence and acceptance criteria, without
starting a worker. The task carries the incident relationship through later
attempts. A failure of the repair links to that relationship and cannot create
a repair-of-the-repair loop.

```yaml
supervision:
  mode: observe
  repair:
    mode: backlog
    repository: example/robert
    max_repairs_per_day: 3
```

For automatic implementation, explicitly select `mode: auto`, a configured
`command` worker, both `push_existing_pr` and `open_pr` in `allowed_actions`, a
trusted `launcher` argv, and approved `verification_commands`. The fixed route
is `new-pr`; incident text cannot choose a route or expand permissions. The
launcher contract is `launcher -- executable args`. It must enforce real OS or
account isolation for both the probe and the coding process. A prompt, an
assertion in configuration, or a private HOME does not satisfy this contract.
Before activation and again on actual dispatch, the probe must fail to read or
write Robert's DB, WAL, configuration and runtime directory while successfully
writing the separate worktree. Place maintenance worktrees outside the source
checkout and runtime directory. The launcher must also deny writes to the source
repository and its common Git directory. Failed verification leaves the proposal
pending.

The probe also checks read access to the controller's GitHub CLI stores
(`~/.config/gh`, `GH_CONFIG_DIR`, and `XDG_CONFIG_HOME/gh`), Git credential files
(`~/.git-credentials` and default/XDG `git/credentials`), and `~/.ssh`. Overrides
must be absolute paths. Existing descendants and symlink targets are checked;
an unreadable or oversized inventory fails closed. The launcher must keep these
entire stores inaccessible throughout coding, including subsequently created
files. Tests use temporary credential markers and never read credential values.
This probe does not verify OS keychains, custom credential helpers or SSH keys
outside those locations; operators must isolate those separately if used.

The inner command receives a JSON packet on stdin and returns one `compact-v1`
`new_pr` submission object on stdout. It has no Robert database access. The
trusted broker binds the task/attempt/output identity, maintains heartbeats and
uses the existing result validation and durable spool. The model only proposes
GitHub actions; audit and publisher perform them. The command worker and
launcher must be administrator-controlled. Normal Codex/Claude coding adapters
are not automatically qualified for this profile. Revoke or change the policy
to stop future dispatch/publication under the old authority.

Preparation renders this stdout submission contract directly, without database
paths, runtime helper commands or helper-acknowledgement requirements. Request
events are included in full: the restricted child cannot read the controller's
context archive. Failed or blocked work must be reported honestly in the same
submission; the broker still validates it as `new_pr`.

Automatic maintenance uses a separate clone, with no shared worktree metadata or
hard-linked objects. Update the configured source repository's base branch before
preparing a repair; preparation uses that local branch. The controller pins its branch, base commit and configured
GitHub remote before launch. Audit and publication import bounded object data into
a private Git directory under the protected runtime directory. They never load
the model's Git configuration, hooks, credential helpers or replace refs. A fresh
index checks actual tracked content, including files hidden with
`assume-unchanged`. Publication pushes the audited commit to the pinned remote.
Old maintenance attempts without this binding are rejected. This path currently
requires SHA-1 Git repositories without submodules and limits each inspection to
128 MiB of objects and 20,000 object files; exceeding those bounds requires
attention, rather than falling back to unrestricted Git. The launcher must grant
writes only to the assigned checkout and private HOME, not the worktree parent.
The controller uses its existing `gh` credentials for the canonical HTTPS GitHub
remote; those credentials never reach the restricted coding command.

For manual code verification, create a separate checkout with `git clone
--no-local`, then detach it at the exact repair commit. A linked Git worktree is
not supported by this verification path.

Inspect and drive the same persisted repair flow with:

```sh
robert supervisor repair-propose --config /path/to/config.yml --incident-id incident-ID
robert supervisor repair-status --config /path/to/config.yml --incident-id incident-ID --output json
robert supervisor repair-verify --config /path/to/config.yml --incident-id incident-ID --checkout /path/to/pinned/worktree --output json
```

The resident supervisor runs proposal and follow-up work in a separate bounded
process. Follow-up executes approved verification commands in the restricted
launcher, inspects GitHub merge/target containment, verifies the running build,
and checks positive original-task progress. It records four distinct stages:
`code_verified`, `mainline_verified`, `deployed_verified`, `original_progress`.
Verification commands run in a newly materialized checkout of the exact audited
commit, so ignored files from the coding workspace cannot change their outcome.
An unchanged verified code snapshot can reuse its test evidence; changed inputs
invalidate that evidence. Pending deployment is rechecked on a durable schedule,
not by rerunning the coding worker. A merged PR alone cannot verify deployment
or close a stalled original task. Cancellation is recorded as not applicable,
not as a successful recovery.

Generate a build sidecar from a clean committed source tree and its actual wheel:

```sh
python3 -m build
robert supervisor build-manifest --repo /path/to/clean/source --wheel /path/to/package.whl --output-file /trusted/build.json
```

Configure `supervision.build_manifest: /trusted/build.json` on installation.
Keep the sidecar outside the checkout and writable only by deployment ownership.
The daemon compares installed package bytes against the sidecar once at startup,
and records its process identity, exact daemon run start and subsequent ticks.
A matching version string is insufficient. Missing provenance remains
`unverified`. The same-host runtime record cannot attest a different host.

## Verification and disabling recovery

The executable offline simulation is separate from the unit suite:

```sh
python3 -m tests.simulations.supervision_scenarios
```

It uses real temporary SQLite locks (including a reaped diagnosis waiting to
persist its result), competing/restarted processes, a durable spool consumer,
local Git/wheel fixtures, OS isolation when available, and two
real temporary Supervisord managers. Loopback HTTP fault injection covers
single/repeated 429 recovery, exhausted primary/fallback models, concurrent
workers, cancellation during backoff, diagnostic cooldown across incidents and
restart, numeric/date/missing/invalid Retry-After, and simultaneous worker and
diagnosis throttling. These HTTP scenarios also run in the unit-suite CI entry
point, using real subprocesses and HTTP requests. It performs no GitHub publication or
production service changes. A platform without an enforceable isolation fixture
reports that check as blocked; it does not substitute a mock success. CI's
existing Python/macOS/Linux matrix covers the deterministic suite.

Daemon health notices remain the fallback when the supervisor is off or coverage
is stale. Fresh independent coverage owns overlapping delay/control alerts.
Disabling recovery stops new actions; outstanding claims still require draining
or reconciliation. Keep receipts, unknown scopes and budgets until their effects
have been verified. No production service or model profile is enabled merely by installing this code.

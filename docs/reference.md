# Reference

[文档目录](README.md)

## Configuration

Default path: `~/.config/robert/config.yml`. Use `robert config path` to resolve
`ROBERT_CONFIG`, or pass `--config` explicitly.

The configuration contract is `version: 2` YAML. Unsupported versions, unknown
fields, duplicate YAML keys and unknown route IDs are errors.
`robert init` creates a minimal v2 file. The [packaged example](../src/robert_agent/resources/config.example.yml)
lists optional settings with an empty repository list; add a real checkout first.
The [schema](../src/robert_agent/resources/schemas/config.schema.json) defines
structural validation and defaults. SQLite is the execution engine.

| Section | Purpose |
| --- | --- |
| `version`, `data_dir`, `database`, `python_bin` | Format, runtime directory, SQLite filename, optional helper interpreter override. |
| `default_worker` | Required worker name; mapping order does not select a default. |
| `github` | Listening account, polling and API quota thresholds; no credentials. |
| `scheduler` | Global task capacity, native model-process capacity, local polling and task leases. |
| `supervision` | Independent observation/diagnosis/recovery settings plus heartbeat staleness, hard timeout and startup grace. |
| `daemon` | Enablement, immediate startup scan, scan/local-cycle control timeouts. |
| `maintenance` | Automatic pruning, interval, batch size and diagnostic retention days. |
| `workers` | Named adapters, commands, model/effort, fallback, timeout and environment allowlist. |
| `skills` | Skill search paths. |
| `routes` | Worker, ordinary automatic model/effort overrides, skills, native review/vote settings. |
| `repos` | Checkouts, worktrees, branch, trust, task capacity, route overrides and opt-in PR automation. |

A complete minimal example follows. Replace repository paths, branch, account
and trusted actor with your deployment values:

```yaml
version: 2
data_dir: ~/.local/share/robert
database: robert.sqlite3
default_worker: coder
github:
  account: robert-bot
workers:
  coder:
    adapter: codex
    command: codex
    model: default
    effort: default
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/worktrees/backend
    default_branch: main
    trusted_actors: [maintainer]
```

Worker `adapter`, `command`, `model` and `effort` are required. The command
adapter requires an argv list. Prompt transport is fixed to stdin.

The [continuous supervision settings](task-supervision.md) live under
`supervision` alongside worker timing fields. The default mode is `observe`;
diagnosis, recovery and repair require explicit configuration.

### Workers, routes, and repository overrides

Automatic execution selects the repository route worker, then the global route
worker, then `default_worker`. Model and effort each use the repository route
override, the global route override, or the selected worker's default.
Explicit manual selection uses that worker's model/effort. Native multi-model
review/vote use each participant's own model/effort; route overrides apply only
to ordinary automatic execution. `config show` includes effective routes and
explains this scope; its normalized output is not a round-trip YAML template.

Skill lists replace the corresponding inherited list; native review/vote
settings replace their entire block. Null does not clear inherited settings.
Packaged required-skill constraints apply. The first native
participant must equal the effective route worker. Trust checks and review
PR-author eligibility are checked independently of worker selection.

`repos[].github_account` overrides the listening account, not gh authentication.
Repository max_concurrency cannot exceed scheduler.max_concurrency. Trusted
actors must be nonempty; these remain repository-scoped authorization settings.

The default hard worker timeout is 45 minutes. When a worker defines
`fallback_model`, Robert uses it on the third run after the two immediately
preceding terminal attempts in the same workstream both end in
`failed_timeout`. A successful attempt or any other failure type resets the
sequence. The fallback changes only the model; the selected worker and effort
remain unchanged.
When a worker exits with a terminal HTTP 429 or rate-limit error, Robert
automatically resumes the same task once using that worker's `fallback_model`,
even if no progress was saved. The switch is persisted across daemon restarts
and consumes the existing one-shot automatic recovery quota. A missing fallback,
the same model configured as fallback, an exhausted quota, or a rate-limited
fallback stops automatic recovery and records the failure. Quoted error text
and errors followed by a successful protocol completion do not trigger a switch.
Native review/vote stages apply the same switch per stage and worker, preserve
their shared prompt and snapshot, and stop if the fallback is also rate limited.
`pr_automation` is optional and defaults to disabled. Enabling CI remediation
requires a non-empty `check_allowlist`; its limits are per durable PR snapshot
episode. Conflict remediation and CI publication use ordinary non-force pushes
only after the current remote PR head and base match the audited snapshot.

### Scheduling and timeout defaults

| Setting | Default | Scope |
| --- | --- | --- |
| `scheduler.max_concurrency` | 3 | Global task slots. |
| `scheduler.model_max_concurrency` | 4 | Shared native review/vote model pool; no aliases. |
| `scheduler.local_poll_seconds` | 5 | Local polling and supervision cadence. |
| `scheduler.lease_ttl_seconds` | 540 | Task lease. |
| `supervision.heartbeat_stale_seconds` | 1200 | Heartbeat staleness. |
| `supervision.hard_timeout_seconds` | 2700 | Global supervision wall-time ceiling. |
| `supervision.startup_grace_seconds` | 300 | Starting worker grace. |
| `github.polling.fallback_seconds` | 300 | Startup/error fallback; normal synchronous interval. |
| `github.polling.synchronous.full_capacity_seconds` | 600 | Synchronous full-capacity interval only. |
| `github.polling.synchronous.rate_limit_cache_seconds` | 300 | Synchronous quota cache only. |
| `github.rate_limit.min_search_remaining` / `min_core_remaining` | 10 / 500 | API quota floors. |
| `daemon.enabled` / `scan_on_start` | true / false | Service enablement and immediate scan. |
| `daemon.control_timeouts.github_scan_seconds` / `local_cycle_seconds` | 300 / 180 | Scan and local-drain/publisher child deadlines. |

The ordinary worker deadline remains the smaller of the supervision ceiling
and its own timeout_seconds. PR automation's max_wall_minutes is a separate
business-episode budget and keeps its existing unit. See [polling](github-polling.md)
for server-directed intervals and [retention](#diagnostic-retention) for protected history.

### Skill examples

If Superpowers skills are installed locally, add their skill root to
`skills.search_paths` and then reference the skill names in route overrides:

```yaml
skills:
  search_paths:
    - ~/.agents/skills
    - ~/.agents/vendor/superpowers/skills
routes:
  new-pr:
    required_skills:
      - superpowers:verification-before-completion
    recommended_skills:
      - superpowers:test-driven-development
  update-existing-pr:
    required_skills:
      - superpowers:verification-before-completion
    recommended_skills:
      - superpowers:receiving-code-review
```

## CLI

Commands and flags are defined by the [CLI parser](../src/robert_agent/cli/main.py).
Use `robert <command> --help` for the installed binary's accepted arguments.

Global:

```bash
robert --help
robert --version
```

Configuration and readiness:

```bash
robert init --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml --output json
robert config validate --config ~/.config/robert/config.yml
robert config show --config ~/.config/robert/config.yml --output json
robert config path
```

Cycles and inspection:

```bash
robert run once --config ~/.config/robert/config.yml
robert run show RUN_ID --config ~/.config/robert/config.yml --output json
robert status --config ~/.config/robert/config.yml
robert task show TASK_ID --config ~/.config/robert/config.yml
robert artifact show TASK_ID ARTIFACT_TYPE --config ~/.config/robert/config.yml
```

Artifact previews read registered regular files inside the database directory.
Paths or symlinks escaping that directory are unavailable.

Operations:

```bash
robert daemon run --config ~/.config/robert/config.yml
supervisord -c ~/.config/robert/supervisord.conf
supervisorctl -c ~/.config/robert/supervisord.conf status
robert web run --config ~/.config/robert/config.yml
robert diagnostics export --output robert-diagnostics.zip
```

Prepare Supervisor configuration using the [operations guide](guides.md#daemon-service).
`robert service install` and `robert service start` select a backend automatically
and default to `--target all` (daemon and task supervisor). Only the Linux
Supervisor backend includes Web in its daemon target.

Integrations:

```bash
robert openclaw install
robert openclaw status
```

## Exit Codes

| Code | Name | Meaning |
| --- | --- | --- |
| `0` | success | Command completed and was not blocked. |
| `2` | retryable | Retry after an external or temporary condition changes. |
| `3` | invalid input | Configuration, arguments, identifiers, or state are invalid. |
| `4` | security refusal | A trust or security boundary refused the operation. |
| `5` | state failure | Durable state or command output could not be read safely. |

Commands print a structured `status` and, on failure, a redacted `safe_error`.

```bash
robert doctor --config ~/.config/robert/config.yml --output json
```

## Database

The default database is `~/.local/share/robert/robert.sqlite3`.

Major table groups:

- repositories, actors, and permissions;
- GitHub sources and normalized events;
- workstreams, tasks, attempts, and route decisions;
- artifacts, worker results, verification, and usage;
- planned GitHub actions and notifications;
- daemon runs, leases, wakeups, work items, and project memory.

Schema initialization is idempotent and applies guarded migrations. Do not edit
rows manually while Robert is running.

```bash
robert status --config ~/.config/robert/config.yml --output json
robert task show TASK_ID --config ~/.config/robert/config.yml --output json
```

Diagnostics exports do not include the raw database.

### Diagnostic retention

The daemon prunes diagnostic history on its first decision and every
`maintenance.interval_seconds` (300 by default), including while it remains
running. Set `maintenance.enabled: false` to disable automatic pruning. This
also disables daemon-event cleanup; the manual command remains available.

Each transaction selects up to `maintenance.batch_size` (default 100, maximum
500) records in each of these categories:

- Completed/skipped `agent_runs` older than `maintenance.retention.runs_days` (7), or failed
  runs older than `maintenance.retention.failed_runs_days` (30), measured from `finished_at`.
  Their global and repository steps are deleted in the same transaction.
- Ordinary daemon events older than `maintenance.retention.daemon_events_days` (7). Failed/degraded
  events use the longer of this window and `maintenance.retention.failed_runs_days`.
- Released leases older than `maintenance.retention.runs_days`, and expired leases older
  than `maintenance.retention.failed_runs_days`, for agent runs and daemons only.

Running or unfinished runs, the latest run, every run referenced by a wakeup,
active leases, and leases owned by running agents/daemons are preserved. Task,
attempt, phase, result, audit, GitHub event/action deduplication, workstream,
work item, and wakeup history is not pruned. Some protected old diagnostics can
therefore remain beyond the retention window. Counts are bounded per category;
a selected run can own multiple step rows.

Normal daemon heartbeats update the current lease without appending an event.
Child completion events retain status, run references and bounded redacted
errors instead of full subprocess stdout and nested cycle results. Successful
`summarize` steps refer to `agent_runs.summary_json`; `robert run show` and the
Web dashboard resolve this reference and also read inline summaries.
Repository-pipeline diagnostics are retained for interrupted runs.

Preview a batch without modifying the database or contacting GitHub:

```bash
robert maintenance prune --config ~/.config/robert/config.yml --output json
```

After reviewing the preview, apply bounded batches to catch up an existing
backlog (at most 1,000 transactions per invocation):

```bash
robert maintenance prune --config ~/.config/robert/config.yml \
  --apply --batch-size 500 --batches 100 --output json
```

The result reports committed row counts and batch count. A lock conflict returns
exit code 2; earlier batches, if any, remain committed, and retrying is safe.
Other database errors return 5. Preview always inspects one batch regardless
of `--batches`. It never creates a missing database or applies schema migrations.
The daemon initializes the supporting indexes on startup.

DELETE frees pages for reuse; it does not immediately shrink the SQLite file.
Robert never runs VACUUM automatically. To obtain a consistent compact copy for
transfer, use SQLite's explicit export to a new destination (requires SQLite
3.27 or newer). Do this after the desired retention batches have completed:

```bash
sqlite3 ~/.local/share/robert/robert.sqlite3 \
  "VACUUM INTO '/tmp/robert-review-compact.sqlite3';"
```

The destination must be absent or empty. This operation needs enough disk space
for the copy and can take time on a large database. The resulting database still
contains private task history; it is a database backup, unlike the redacted
`robert diagnostics export` archive. To reclaim space in the live database,
stop its writers and run VACUUM during a maintenance window.

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `ROBERT_CONFIG` | Override `~/.config/robert/config.yml`. |
| `ROBERT_DATA_DIR` | Override the default data directory used by initialization and service runtime paths; existing YAML `data_dir` must be kept consistent. |

Worker subprocesses receive `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`,
`SHELL`, and `USER`. Additional names must be listed in
`environment_allowlist`; values stay in the host environment. GitHub credential
names (`GH_TOKEN`, `GITHUB_TOKEN`, and their enterprise variants) are rejected
in model-worker allowlists. Native coordinators retain their controller credentials
for metadata reads and snapshot preparation.

Workers do not accept an `env` or `environment` mapping. For non-secret values,
an explicit command prefix such as
`command: ["env", "IS_SANDBOX=1", "tclaude"]` sets the variable for that worker
without an allowlist entry. Use this particular prefix only for root execution
in an already restricted container; see the
[Claude container deployment guide](guides.md#claude-workers-as-root-in-a-restricted-container).
Keep credentials out of commands and YAML.

```bash
ROBERT_CONFIG=/etc/robert/config.yml \
ROBERT_DATA_DIR=/var/lib/robert \
robert doctor --output json
```

`--config` takes precedence over `ROBERT_CONFIG`. An existing configuration's
`data_dir` determines its task database; setting `ROBERT_DATA_DIR` does not
rewrite that YAML. Keep both processes on the same configuration and data paths.

Do not place GitHub tokens or OpenClaw credentials in configuration.

## Recovery safeguards

Review and analysis workspaces are refreshed only when the registered directory
is inside the configured worktree root and has no local changes. A dirty or
unmanaged workspace blocks execution instead of discarding files. Commit or
preserve those changes before retrying. Repeated analysis uses the refreshed
upstream base rather than the first analysis snapshot.

Worker timeout and remote Issue/PR closure terminate the worker process group,
including descendants whose leader has exited. Cleanup escalates from SIGTERM
to SIGKILL. If cleanup cannot be confirmed, Robert retains task ownership and
capacity for another supervision pass and records a `worker_cleanup_pending`
notification. Missing or invalid worker PIDs require operator investigation.

Publication records the controller-observed result commit and fork owner in the
audit scope. A retry recognizes an existing-PR push that already reached that
commit, while a different remote head remains protected by the lease. Pending
pushes without an audited result SHA fail with an explicit message; retry the task to obtain a new audited result. PR creation and
deduplication must agree with the assigned fork owner even when a workstream
marker matches.

An exhausted repair stays stopped when later polls observe the same failed
result snapshot. A new head or base may start a new remediation episode.
Configured required skills also apply to classification follow-ups, pending
context children, prepared-task reroutes, and local work items. Canceled work items cannot be started
or resumed by later commands; completed items must be reopened first.

### Bounded recovery and delivery

Each daemon control lane retries failures at most three times, with exponential
backoff starting at `scheduler.local_poll_seconds`. A successful cycle resets that
lane's budget. Exhaustion records a local operator notice and stops that lane;
other lanes continue. The daemon stays resident and renews its lease so a service
manager cannot turn a permanent failure into a restart loop. Repair the cause,
then restart the daemon to resume the lane. An unidentifiable result-spool or
database-inspection blocker stops all automatic execution immediately.
While new execution is paused, existing scan and publication children are still
polled, timed out and reaped, and their outcomes are recorded. A child's completion
does not clear an operator stop; repair and restart are still required.

An incompatible result associated with a known task instead isolates that task
and puts its card in Waiting for You. Incompatible results and their pending
publications remain preserved and cannot be consumed or published. Unrelated
tasks continue. Recorded workers keep their capacity until process exit is proven;
uncertain identities require operator cleanup. Once workers have stopped and
pending publications have been reconciled, use Retrigger on the main board or task detail
to replace an isolated GitHub task with refreshed source context. The operator
request closes the old execution and preserves its results, events, and attention
history. Other affected work can be canceled and replaced after reconciliation.
Isolated workers continue to receive exit checks even during a global stop;
confirmed exits release capacity without ingesting or rewriting their results.
Ordinary Approve cannot resolve protocol isolation or create recovery work.
Internal helper scripts use Robert's own interpreter. The configured `python_bin`
is validated separately; it does not replace the interpreter used for those helpers.

Worker authentication failures (`401`, `Unauthorized`, expired credentials) stop
recovery immediately and expose an operator action. Other dead-worker recovery
keeps the existing one-shot quota. A stopped, confirmed-dead GitHub task can be
retried explicitly from the workbench; this creates a new task and keeps the old
workflow terminal. Uncertain processes or unreconciled publication require
inspection/cancellation first.

GitHub publication retries transient failures at most three times over a maximum
10-minute retry window, with persisted exponential backoff. Permanent credential,
permission, and branch-conflict failures stop immediately. The budget is charged
before the external effect, including an ambiguous crash. Later actions from the
same result wait for earlier failed publication. Retry state and required actions
are available on the board and workbench. Do not force-push to recover a rejected
diverged-branch action; obtain a fresh audited result against the current branch.

Final Worker results are atomically spooled in `<database>.results/` before SQLite
ingestion. The daemon and task cycle replay pending files; identical submissions
are deduplicated by attempt and payload. Canceled or obsolete results cannot
resume execution. Rejected and corrupt files are retained for inspection, with
operator notifications; they are not automatically deleted. Keep this directory
with the database during backup. Waiting-for-user results require a
structured question, and GitHub tasks additionally require a public comment action.

Set `ROBERT_NOTIFICATION_COMMAND` in the daemon service environment to enable
external operator delivery. It is parsed as an argument vector (no shell), receives
one redacted JSON object on stdin, and must exit zero on success within five
seconds. The payload includes a stable `delivery_id`; receivers must deduplicate
it because a crash after send can cause redelivery. Failures have a persisted
three-attempt/10-minute budget. Without configuration, notifications remain local;
recording a notification does not mean it was delivered. Delivery attempts run
outside database transactions with a bounded per-cycle time budget. Health events
report whether delivery is configured; notification metadata records its outcome.

Maintenance records `reliability_health` events containing database/WAL/reusable
bytes and bounded samples of delayed tasks, pending notifications and stale runs.
Queue/publication delays over 10 minutes produce one local notice per task.
These are diagnostics, not automatic deletion of potentially live work. Existing
retention and offline compaction instructions above still apply. Run-step times
use actual boundary timestamps and monotonic durations; Codex
`turn.completed` usage is recognized.

### Multi-Agent voting

The opt-in `vote` route accepts explicit trusted mentions on Issues and PRs,
prepares one fixed question/options/context, collects independent equal ballots,
and posts a source-preserving report to the triggering conversation. See
[Multi-Agent voting](multi-agent-vote.md) for configuration, abstentions, round
isolation, recovery and publication rules.

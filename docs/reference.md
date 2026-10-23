# Reference

## Configuration

Default path: `~/.config/robert/config.yml`. Use `robert config path` to resolve
`ROBERT_CONFIG`, or pass `--config` explicitly.

The public configuration is `version: 1` YAML. `robert init` creates a starting
file; the [packaged example](../src/robert_agent/resources/config.example.yml)
and [schema](../src/robert_agent/resources/schemas/config.schema.json) describe
the supported structure. The example has `repos: []` as a template: add at least
one real local Git checkout before validation or operation.

| Section | Purpose |
| --- | --- |
| `version`, `data_dir`, `database` | Format version, runtime directory, and SQLite filename. `database` must be a filename, not a path. |
| `github` | Listening account and poll interval; uses the existing authenticated `gh` session. |
| `workers` | Mapping of named adapters, commands, models, effort, timeouts, and environment allowlists. |
| `skills` | Local skill search paths; Robert validates skills but does not install them. |
| `routes` | Global worker and required/recommended skill overrides. |
| `route_worker_models` | Per-route model/effort overrides, separate from route permissions. |
| `repos` | Real checkouts, worktree roots, base branches, trusted actors, concurrency, route overrides, and opt-in PR automation. |
| `daemon` | Local/GitHub polling, rate-limit thresholds, cycle timeouts, and diagnostic retention. |
| `workflow_engine` | Workflow migration mode; normal operation uses `legacy`. |

The following is an extended example, not the generated defaults. Replace the
accounts, paths, branch names, commands, and model names for your environment.
The example explicitly enables PR automation on `example/backend`; omit that
block to keep automation disabled.

```yaml
version: 1
data_dir: ~/.local/share/robert
database: robert.sqlite3
python_bin: python3
max_concurrency: 3
stale_after_minutes: 20
hard_timeout_minutes: 45
worker_startup_grace_seconds: 300
lease_ttl_minutes: 9
github:
  account: robert-bot
  poll_seconds: 300
skills:
  search_paths:
    - ~/.agents/skills
daemon:
  enabled: true
  local_poll_seconds: 5
  github_poll_seconds: 300
  github_poll_when_full_seconds: 600
  rate_limit_cache_seconds: 300
  min_search_remaining: 10
  min_core_remaining: 500
  live_run_timeout_seconds: 300
  local_drain_timeout_seconds: 180
  event_retention_days: 7
  maintenance_enabled: true
  maintenance_interval_seconds: 300
  maintenance_batch_size: 100
  run_retention_days: 7
  failed_run_retention_days: 30
  run_on_start: false
workflow_engine:
  mode: legacy
  system_database_url_env: ROBERT_DBOS_SYSTEM_DATABASE_URL
workers:
  default:
    adapter: codex
    command: codex
    model: default
    fallback_model: gpt-5.6-sol
    effort: default
    prompt_transport: stdin
    timeout_seconds: 5400
    environment_allowlist: []
  reviewer:
    adapter: command
    command: [custom-reviewer, --batch]
    model: default
    effort: default
    prompt_transport: stdin
    timeout_seconds: 3600
    environment_allowlist:
      - CUSTOM_REVIEWER_HOME
route_worker_models:
  new-pr:
    worker: default
    model: gpt-5.4
    effort: high
routes:
  new-pr:
    worker: default
    required_skills: []
    recommended_skills: []
  review-pr:
    worker: reviewer
    required_skills: []
    recommended_skills: []
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/repos/backend/.worktrees
    default_branch: main
    github_account: robert-bot
    trusted_actors:
      - maintainer
    max_concurrency: 2
    pr_automation:
      conflict:
        enabled: true
        max_attempts: 2
        max_wall_minutes: 60
      ci:
        enabled: true
        check_allowlist: [unit, lint]
        max_attempts: 2
        max_wall_minutes: 120
        max_total_tokens: 200000
        max_cost_usd: 10.0
        max_failure_summary_chars: 12000
    routes:
      new-pr:
        worker: default
        required_skills: []
        recommended_skills: []
  - full_name: example/frontend
    checkout: /srv/repos/frontend
    worktrees: /srv/repos/frontend/.worktrees
    default_branch: main
    trusted_actors:
      - maintainer
```

### Workers, routes, and repository overrides

`workers` is a mapping keyed by worker name. Each entry defines `adapter`,
`command`, `model`, and `effort`; optional fields are `fallback_model`,
`prompt_transport` (only `stdin`), `timeout_seconds` (default 5400), and
`environment_allowlist` (default empty). Built-in adapters are `codex`,
`tcodex`, `claude`, `tclaude`, `opencode`, `cbc`, and `command`.

`github` accepts only `account` and `poll_seconds`; credentials are rejected.
The `command` adapter requires a YAML sequence. Repository route overrides may
set `worker`, `required_skills`, and `recommended_skills` only. They replace
only fields explicitly supplied; a supplied skill list replaces that list.
Repository overrides take precedence over global `routes`, then packaged
policy defaults. With no selected worker, the first configured worker is used.

Use `routes.<route>.worker` to select the worker for `version: 1`. Set
`route_worker_models.<route>.model` and `.effort` for per-route model overrides;
these do not belong inside `routes` or repository route overrides.
`route_worker_models` also accepts `worker`, which supplies defaults when its
model/effort are omitted; public versioned configuration still selects the
executed worker through `routes`. For manual worker selection on a local task,
the selected worker's own model/effort apply instead of the route model override.

A repository's `github_account` overrides the global listening account without
changing `gh` authentication. Its `max_concurrency` must be between 1 and the
global `max_concurrency` (default 3). The checkout must already exist as a Git
repository, `default_branch` must name its actual base branch, and
`trusted_actors` must be non-empty.

The default hard worker timeout is 45 minutes. When a worker defines
`fallback_model`, Robert uses it on the third run after the two immediately
preceding terminal attempts in the same workstream both end in
`failed_timeout`. A successful attempt or any other failure type resets the
sequence. The fallback changes only the model; the selected worker and effort
remain unchanged.
`pr_automation` is optional and defaults to disabled. Enabling CI remediation
requires a non-empty `check_allowlist`; its limits are per durable PR snapshot
episode. Conflict remediation and CI publication use ordinary non-force pushes
only after the current remote PR head and base match the audited snapshot.

```bash
robert config validate --config ~/.config/robert/config.yml --output json
```

### Scheduling and timeout defaults

| Setting | Default | Scope |
| --- | --- | --- |
| `max_concurrency` | 3 | Global active-worker limit. |
| `stale_after_minutes` | 20 | Worker heartbeat staleness threshold. |
| `hard_timeout_minutes` | 45 | Supervision wall-time limit, independent of worker adapter timeout. |
| `worker_startup_grace_seconds` | 300 | Grace period for a starting worker. |
| `lease_ttl_minutes` | 9 | Task lease duration. |
| `python_bin` | `python3` | Python executable used for child commands. |
| `daemon.local_poll_seconds` | 5 | Local state/supervision polling. |
| `daemon.github_poll_seconds` | 300 | Daemon GitHub polling interval; configured independently of `github.poll_seconds`. |
| `daemon.github_poll_when_full_seconds` | 600 | GitHub polling while worker capacity is full. |
| `daemon.rate_limit_cache_seconds` | 300 | Rate-limit cache duration. |
| `daemon.min_search_remaining` / `min_core_remaining` | 10 / 500 | Minimum GitHub quota thresholds. |
| `daemon.live_run_timeout_seconds` / `local_drain_timeout_seconds` | 300 / 180 | Bounded live-poll/local-drain child cycle timeouts. |
| `daemon.enabled` / `run_on_start` | `true` / `false` | Daemon enablement and immediate startup poll. |

Increasing only `workers.<name>.timeout_seconds` does not raise the global
hard timeout. Retention defaults and protected history are described under
[Diagnostic retention](#diagnostic-retention).

### Workflow engine

```yaml
workflow_engine:
  mode: legacy
  system_database_url_env: ROBERT_DBOS_SYSTEM_DATABASE_URL
```

`legacy` is the default and does not require a DBOS database URL. The validator
also accepts `shadow` and `active`; both require the named environment variable
to contain a `postgresql://` URL. Store only the environment variable's **name**
in YAML. This PostgreSQL system database is separate from Robert's SQLite task
and audit database.

DBOS runtime, bindings, durable workflows, and recovery foundations are present,
but production dispatch still retains legacy ownership and active production
drivers are not wired in. Keep `legacy` for unattended operation. A valid
`active` configuration is not evidence of a supported production cutover. See
[the lifecycle integration notes](web-lifecycle-board.md#与主工作区已有恢复逻辑的集成).

### Updating an existing configuration

Preserve the current file and runtime data before editing. `robert init` refuses
to overwrite an existing file without `--force`; it does not migrate or merge
custom settings. Prefer editing the existing YAML and validating it.

For the public `version: 1` layout, use `github.account`, named `workers`,
nested `daemon` settings, and repository keys `checkout`, `worktrees`, and
`default_branch`. Do not combine named workers with old top-level
`worker_agent` / `worker_command`, or copy normalized output keys such as
`github_account`, `repo_root`, `worktree_root`, and `default_base_branch` into
public configuration. Validation/show output includes internal normalized
fields; it is not a round-trip configuration template.

Missing `workflow_engine` defaults to `legacy`, and omitted optional daemon
settings receive their defaults. Keep PR automation disabled unless explicitly
wanted. After editing:

```bash
robert config validate --config ~/.config/robert/config.yml --output json
robert doctor --config ~/.config/robert/config.yml --output json
```

Then restart both daemon and web using the [Supervisor guide](guides.md#daemon-service).
Supervisor process definitions, Web flags, and autostart belong in
`supervisord.conf`, not in Robert YAML. Web bind/port/write settings are CLI
flags, not a `web:` configuration section.

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

Operations:

```bash
robert daemon run --config ~/.config/robert/config.yml
supervisord -c ~/.config/robert/supervisord.conf
supervisorctl -c ~/.config/robert/supervisord.conf status
robert web run --config ~/.config/robert/config.yml
robert diagnostics export --output robert-diagnostics.zip
```

Prepare Supervisor configuration using the [operations guide](guides.md#daemon-service).
`robert service install` and `robert service start` remain alternatives with
automatic backend selection; systemd/launchd only manage the daemon.

Integrations and migration:

```bash
robert openclaw install
robert openclaw status
robert migrate dd-github-agent --dry-run
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
`maintenance_interval_seconds` (300 by default), including while it remains
running. Set `maintenance_enabled: false` to disable automatic pruning. This
also disables daemon-event cleanup; the manual command remains available.

Each transaction selects up to `maintenance_batch_size` (default 100, maximum
500) records in each of these categories:

- Completed/skipped `agent_runs` older than `run_retention_days` (7), or failed
  runs older than `failed_run_retention_days` (30), measured from `finished_at`.
  Their global and repository steps are deleted in the same transaction.
- Ordinary daemon events older than `event_retention_days` (7). Failed/degraded
  events use the longer of this window and `failed_run_retention_days`.
- Released leases older than `run_retention_days`, and expired leases older
  than `failed_run_retention_days`, for agent runs and daemons only.

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
Web dashboard resolve this reference, while older inline summaries remain
readable. Repository-pipeline diagnostics are retained for interrupted runs.

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
The daemon initializes the supporting indexes on startup after upgrade.

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
`environment_allowlist`; values stay in the host environment.

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
commit, while a different remote head remains protected by the lease. Older
pending pushes without an audited result SHA fail with an explicit
message; retry the task to obtain a new audited result. PR creation and
deduplication must agree with the assigned fork owner even when a workstream
marker matches.

An exhausted repair stays stopped when later polls observe the same failed
result snapshot. A new head or base may start a new remediation episode.
Configured required skills also apply to classification follow-ups, pending
context children, prepared-task reroutes, and local work items. Canceled work items cannot be started
or resumed by later commands; completed items must be reopened first.

### Bounded recovery and delivery

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
with the database during backup or migration. Waiting-for-user results require a
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
now use actual boundary timestamps and monotonic durations; Codex
`turn.completed` usage is recognized.

### DBOS production v2

With `workflow_engine.mode: active`, new eligible GitHub tasks and remediation
episodes bind to v2 before any legacy dispatch. The daemon installs a production
driver before launching DBOS; it supervises Workers, audits results, publishes
through existing intents, and finalizes the domain state. Existing legacy subjects
continue to drain under legacy ownership. Web-origin work remains on its existing
path. No subject changes owner during rollout.

v1 workflow definitions and IDs remain intact. Existing active DBOS v1 subjects
must drain with their original driver before this production v2 mode can start;
the readiness check refuses to reinterpret their recorded history. A persisted
ambiguous dispatch fence stops the v2 subject for inspection instead of spawning
a second Worker. Validate rollout against a staging database and the intended
Worker/service configuration before enabling active mode in production.

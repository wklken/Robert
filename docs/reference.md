# Reference

## Configuration

Default path: `~/.config/robert/config.yml`.

```yaml
version: 1
data_dir: ~/.local/share/robert
database: robert.sqlite3
python_bin: python3
max_concurrency: 3
stale_after_minutes: 20
hard_timeout_minutes: 90
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
workers:
  default:
    adapter: codex
    command: codex
    model: default
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

`github` accepts only `account` and `poll_seconds`; credentials are rejected.
The `command` adapter requires a YAML sequence. Repository route overrides may
set `worker`, `required_skills`, and `recommended_skills` only.
`pr_automation` is optional and defaults to disabled. Enabling CI remediation
requires a non-empty `check_allowlist`; its limits are per durable PR snapshot
episode. Conflict remediation and CI publication use ordinary non-force pushes
only after the current remote PR head and base match the audited snapshot.

```bash
robert config validate --config ~/.config/robert/config.yml --output json
```

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
robert service install --config ~/.config/robert/config.yml
robert service start
robert web run --config ~/.config/robert/config.yml
robert diagnostics export --output robert-diagnostics.zip
```

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
| `ROBERT_DATA_DIR` | Override `~/.local/share/robert/`. |

Worker subprocesses receive `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`,
`SHELL`, and `USER`. Additional names must be listed in
`environment_allowlist`; values stay in the host environment.

```bash
ROBERT_CONFIG=/etc/robert/config.yml \
ROBERT_DATA_DIR=/var/lib/robert \
robert doctor --output json
```

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

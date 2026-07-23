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
daemon_enabled: true
daemon_local_poll_seconds: 5
daemon_github_poll_seconds: 300
daemon_github_poll_when_full_seconds: 600
daemon_rate_limit_cache_seconds: 300
daemon_min_search_remaining: 10
daemon_min_core_remaining: 500
daemon_live_run_timeout_seconds: 300
daemon_local_drain_timeout_seconds: 180
daemon_event_retention_days: 7
daemon_run_on_start: false
github:
  account: robert-bot
  poll_seconds: 300
skills:
  search_paths:
    - ~/.agents/skills
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
    required_skills:
      - fast-add-tests
    recommended_skills:
      - fast-preflight
  review-pr:
    worker: reviewer
    required_skills: []
    recommended_skills:
      - fast-review-github-pr
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/repos/backend/.worktrees
    default_branch: main
    github_account: robert-bot
    trusted_actors:
      - maintainer
    max_concurrency: 2
    routes:
      new-pr:
        worker: default
        required_skills:
          - fast-add-tests
        recommended_skills:
          - fast-test-fix
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
      - fast-small-pr
  update-existing-pr:
    required_skills:
      - superpowers:verification-before-completion
    recommended_skills:
      - superpowers:receiving-code-review
      - fast-verify-review-point
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

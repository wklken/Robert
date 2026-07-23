# Reference

## Configuration

Default path: `~/.config/robert/config.yml`.

```yaml
version: 1
data_dir: ~/.local/share/robert
database: robert.sqlite3
github:
  account: robert-bot
  poll_seconds: 300
skills:
  search_paths: []
workers:
  default:
    adapter: codex
    command: codex
    model: default
    effort: default
    prompt_transport: stdin
    timeout_seconds: 5400
    environment_allowlist: []
routes: {}
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/repos/backend/.worktrees
    default_branch: main
    trusted_actors: [maintainer]
```

`github` accepts only `account` and `poll_seconds`; credentials are rejected.
The `command` adapter requires a YAML sequence. Repository route overrides may
set `worker`, `required_skills`, and `recommended_skills` only.

```bash
robert config validate --config ~/.config/robert/config.yml --output json
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

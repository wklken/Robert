# Guides

## Installation

```bash
pipx install robert-github-agent
gh auth login
robert --version
robert init --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml
```

Runtime state is written to `~/.local/share/robert/`. Linux and macOS are
supported directly. On Windows, run Robert inside WSL.

For development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

## First Repository

Create the initial configuration interactively:

```bash
robert init --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml --output json
```

Or use non-interactive setup:

```bash
robert init --non-interactive \
  --config ~/.config/robert/config.yml \
  --repo example/backend \
  --repo-path /srv/repos/backend \
  --worker codex \
  --github-account robert-bot \
  --trusted-actor maintainer
```

Preview one cycle:

```bash
robert run once --config ~/.config/robert/config.yml --dry-run --skip-external
```

## Multiple Repositories

```yaml
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/repos/backend/.worktrees
    default_branch: main
    trusted_actors: [maintainer]
  - full_name: example/frontend
    checkout: /srv/repos/frontend
    worktrees: /srv/repos/frontend/.worktrees
    default_branch: main
    trusted_actors: [maintainer]
```

```bash
robert config validate --config ~/.config/robert/config.yml
robert status --config ~/.config/robert/config.yml --output json
```

Each worker receives the workspace belonging to its task's repository.

## Worker Adapters

```yaml
workers:
  default:
    adapter: codex
    command: codex
    model: default
    effort: default
    prompt_transport: stdin
    timeout_seconds: 5400
    environment_allowlist: []
```

Built-in adapters are `codex`, `tcodex`, `cbc`, and `command`. The generic
adapter requires a YAML command sequence:

```yaml
workers:
  custom:
    adapter: command
    command: [custom-agent, --batch]
    model: default
    effort: default
```

## External Skills

Robert validates local skills but does not install or update them:

```yaml
skills:
  search_paths:
    - ~/.agents/skills
routes:
  new-pr:
    required_skills: []
    recommended_skills: [fast-add-tests]
```

Missing required skills block before workspace creation. Missing recommended
skills appear as diagnostics.

```bash
robert doctor --config ~/.config/robert/config.yml --output json
```

## Daemon Service

```bash
robert service install --config ~/.config/robert/config.yml
robert service start
robert service status
```

Linux uses `~/.config/systemd/user/robert.service`; macOS uses
`~/Library/LaunchAgents/dev.robert.agent.plist`.

```bash
robert service install \
  --config ~/.config/robert/config.yml \
  --dry-run --output json
robert daemon run --config ~/.config/robert/config.yml
```

## Local Web UI

Start the read-only server:

```bash
robert web run --config ~/.config/robert/config.yml
```

Enable local writes explicitly:

```bash
robert web run \
  --config ~/.config/robert/config.yml \
  --writable \
  --operator "$USER"
```

Remote binding requires `--allow-remote` and an authenticated reverse proxy.
Writable requests require `X-Robert-CSRF-Token`. Artifact previews read only
paths registered in SQLite.

## OpenClaw

```bash
robert openclaw install
robert openclaw status
```

Preview external commands:

```bash
robert openclaw install \
  --plugin-dir ~/.local/share/robert/openclaw-plugin/robert-openclaw \
  --dry-run --output json
```

The plugin exposes `/robert-status`, `/robert-task`, `/robert-run`, and
`/robert-artifact`. It never schedules or starts Robert.

```bash
robert openclaw uninstall --dry-run
robert openclaw uninstall
```

## Migration

The legacy source defaults to `~/.agents/data/dd-github-agent`; the target is
`~/.local/share/robert/`.

```bash
robert migrate dd-github-agent \
  --source ~/.agents/data/dd-github-agent \
  --target ~/.local/share/robert \
  --dry-run

robert migrate dd-github-agent \
  --source ~/.agents/data/dd-github-agent \
  --target ~/.local/share/robert
```

Migration creates a sibling backup, converts YAML, renames the SQLite account
column, and preserves compatibility with legacy idempotency markers.

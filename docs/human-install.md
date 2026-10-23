# Human Install

This guide is the manual installation path for operators who are not asking a
terminal coding agent to install Robert for them. If a coding agent is doing the
setup, use `docs/agent-install.md` instead.

## Requirements

- Linux or macOS. Windows is supported through WSL.
- Python 3.10 or newer.
- Git and GitHub CLI (`gh`) with an authenticated session.
- At least one local worker command such as Codex.
- `pipx` is recommended for installation.

## Quick Start

```bash
pipx install robert-github-agent --include-deps
gh auth login
robert init
robert config validate
robert doctor
```

The configuration path is `~/.config/robert/config.yml`. Runtime data defaults
to `~/.local/share/robert/`. Review the generated
[configuration layout](reference.md#configuration), including the named worker's
adapter, real repository branch, and trusted actors.

Then follow the [Supervisor setup](guides.md#daemon-service) to create
`~/.config/robert/supervisord.conf` for both daemon and web. After readiness
checks:

```bash
supervisord -c ~/.config/robert/supervisord.conf
supervisorctl -c ~/.config/robert/supervisord.conf status
```

Confirm both programs are `RUNNING`, visit `http://127.0.0.1:8765/`, and check
`/operations` for daemon activity. The recommended example binds Web locally
and keeps it read-only; enable writable mode explicitly when needed.

`robert service install` / `robert service start` are an alternative with an
automatically selected backend. systemd and launchd manage only the daemon;
the Linux Supervisor fallback manages both, with different Web defaults.
Read the backend table in the Supervisor guide before choosing this path.

## Safer First Run

For a first repository, prefer validating the configuration before starting the
service:

```bash
robert init --config ~/.config/robert/config.yml
robert config validate --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml --output json
robert run once --config ~/.config/robert/config.yml --dry-run --skip-external
```

Start unattended operation only after the dry run and doctor output match the
repository, trusted actor, and worker you intended to configure.

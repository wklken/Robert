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
pipx install robert-github-agent
gh auth login
robert init
robert doctor
robert service install
robert service start
```

The configuration path is `~/.config/robert/config.yml`. Runtime data defaults
to `~/.local/share/robert/`.

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

# Install Robert with a Coding Agent

This guide is written for terminal coding agents such as Codex or Claude Code.
Read the entire guide before executing commands.

## Goal

- Install `robert-github-agent`.
- Configure Robert for one existing local GitHub repository.
- Validate GitHub CLI authentication and the configured worker.
- Install the native user service.
- Start unattended operation only after explicit user confirmation.

## Safety Rules

- Do not print, copy, request, or store GitHub tokens, API keys, cookies, or
  credentials.
- Robert must use the existing authenticated `gh` CLI session.
- If `gh auth status` fails, stop and ask the user to run `gh auth login`.
- Do not modify Robert's source or the target repository's source.
- Do not create GitHub issues, comments, pull requests, tags, releases, or
  other remote writes during setup.
- Do not overwrite `~/.config/robert/config.yml` without explicit approval.
- Do not use `--force` unless the user approves it after reviewing the existing
  configuration.
- Explain any system-package installation before running it.
- Show commands before execution and stop on failed verification.

## Required Values

Determine safely or ask the user for:

1. GitHub repository in `owner/name` form.
2. Absolute path to its existing local Git checkout.
3. GitHub account Robert runs as and listens for.
4. Trusted human GitHub actor allowed to trigger Robert.
5. Worker command or adapter, such as `codex`.

Do not assume that the Robert account and trusted human actor are the same
account.

## Setup Procedure

### 1. Inspect the Environment

Detect Linux, macOS, or WSL. Check:

```bash
python3 --version
git --version
gh --version
pipx --version
```

Python must be 3.10 or newer. Also verify the selected worker command:

```bash
command -v codex
```

Replace `codex` if another worker was selected.

### 2. Verify GitHub Authentication

```bash
gh auth status
```

Do not display credential files or environment variables. If authentication
fails, stop and ask the user to run:

```bash
gh auth login
```

### 3. Install pipx When Needed

If `pipx` is missing, explain the platform-supported installation command and
ask before installing a system package. Ensure the pipx binary directory is on
`PATH`.

### 4. Install Robert

```bash
pipx install robert-github-agent
```

If already installed, report the current version and ask before upgrading.

```bash
robert --version
robert --help
```

### 5. Inspect Existing Configuration

```bash
test -f ~/.config/robert/config.yml
```

If the file exists, do not overwrite it. Run validation and report its current
state:

```bash
robert config validate \
  --config ~/.config/robert/config.yml \
  --output json
```

Ask before replacing or changing the file.

### 6. Create Configuration

When no configuration exists, run one correctly quoted command:

```bash
robert init --non-interactive \
  --config ~/.config/robert/config.yml \
  --repo OWNER/REPO \
  --repo-path /absolute/repository/path \
  --worker WORKER \
  --github-account ROBERT_ACCOUNT \
  --trusted-actor TRUSTED_ACTOR
```

Use the values confirmed by the user.

### 7. Validate Readiness

```bash
robert config validate \
  --config ~/.config/robert/config.yml \
  --output json

robert doctor \
  --config ~/.config/robert/config.yml \
  --output json
```

Do not claim success if a required check failed or was skipped.

### 8. Preview and Install the Native Service

Preview:

```bash
robert service install \
  --config ~/.config/robert/config.yml \
  --dry-run \
  --output json
```

If validation passed, install:

```bash
robert service install --config ~/.config/robert/config.yml
```

### 9. Confirm Before Starting

Explain which repository and GitHub events Robert will monitor. Ask the user
for explicit confirmation before starting unattended operation.

Only after confirmation:

```bash
robert service start
robert service status --output json
```

## Final Report

Report:

- Robert version.
- Configuration path: `~/.config/robert/config.yml`.
- Runtime data path: `~/.local/share/robert/`.
- Repository and worker selected.
- Doctor and service status.
- Every skipped, blocked, or failed check.

Success means Robert is installed, configuration is valid, doctor checks pass,
the native service is installed, and no GitHub write occurred during setup.

# Install Robert with a Coding Agent

This guide is written for terminal coding agents such as Codex or Claude Code.
Read the entire guide before executing commands.

## Goal

- Install `robert-github-agent`.
- Configure Robert for one existing local GitHub repository.
- Validate GitHub CLI authentication and the configured worker.
- Prepare Supervisor to manage both daemon and web.
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
5. Worker command or adapter, such as `codex`, and any model/effort requirements.
6. Read-only or writable Web, operator identity if writable, and whether access
   is local or through an authenticated reverse proxy. Default to local read-only
   access until the user chooses otherwise.
7. Any existing Robert service or Supervisor configuration that must be
   preserved or stopped before starting this instance.

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

Check the intended Robert service account as well. For `claude` or `tclaude`
running as root in an already restricted container, follow the
[root container configuration](guides.md#claude-workers-as-root-in-a-restricted-container)
before readiness checks. Preserve the configured adapter, executable path, and
arguments when adding the command prefix; do not infer container isolation
from `IS_SANDBOX=1` itself.

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
pipx install robert-github-agent --include-deps
```

`--include-deps` exposes `supervisord` and `supervisorctl` from Robert's
Supervisor dependency. If already installed, report the current version and ask
before upgrading. If those commands are not exposed, locate Robert's environment
with `pipx list` and use its `bin/supervisord` and `bin/supervisorctl` directly;
do not reinstall just to obtain PATH entries.

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

Use the values confirmed by the user. Read the generated YAML and compare it
with the [configuration reference](reference.md#configuration):

- `github.account` is the listening account, while `repos[].trusted_actors`
  lists humans permitted to trigger work. Confirm the real `default_branch`.
- `workers` is a named mapping. Verify `adapter`, executable `command`, model,
  and effort. `init --worker` only auto-selects `codex`, `tcodex`, or `cbc`;
  for Claude, OpenCode, and custom commands, correct the adapter explicitly.
- `routes` selects Worker/Skill policy; `route_worker_models` supplies route
  model/effort overrides. Do not put those model keys into `routes`.
- SQLite is the only execution engine. Old DBOS `active`/`shadow` deployments
  must complete the [offline upgrade](sqlite-state-machine-upgrade.md)
  before starting this version. An absent block or `mode: sqlite` needs no
  external database; `legacy` is a deprecated compatibility alias.
- Preserve the nested `daemon` section and its retention defaults. Enable
  per-repository `pr_automation` only when requested; CI also needs a non-empty
  `check_allowlist`.
- Keep `data_dir` consistent between daemon and web. Web options are command
  flags, not YAML keys. Do not copy internal normalized validation output back
  into the public configuration.

For an existing file, propose any necessary edits and obtain approval before
applying them. Do not use `robert init --force` as an automatic migration.

### 7. Validate Readiness

```bash
robert config validate \
  --config ~/.config/robert/config.yml \
  --output json

robert doctor \
  --config ~/.config/robert/config.yml \
  --output json
```

Preview one cycle without external discovery or publication:

```bash
robert run once --config ~/.config/robert/config.yml --dry-run --skip-external
```

Do not claim success if a required check failed or was skipped. This preview
alone does not prove live GitHub polling or worker execution.

For root container Claude workers, version checks and mocked launches also
miss the upstream permission-mode check. When live execution is authorized,
verify a newly created task starts the configured Claude worker after restart;
otherwise report that check as pending without creating remote test objects.

### 8. Prepare Supervisor for Daemon and Web

Follow the complete [Supervisor procedure](guides.md#daemon-service), including
its `supervisord.conf` example. Read that section before writing the file.

- Resolve actual executable and home paths; create the required `run` and
  `logs` directories under the intended runtime directory.
- Show the proposed configuration, including both `robert-daemon` and
  `robert-web`, and preserve any existing Supervisor setup. Ask before replacing
  an existing configuration.
- Use the same explicit `--config` path for both programs. Start with Web on
  `127.0.0.1:8765`, read-only; use `--writable --operator ...` only if requested.
  Remote binding also requires `--allow-remote` and an authenticated proxy.
- Verify the start environment can locate Git, `gh`, Python, and the worker.
  Do not put credentials into Supervisor configuration.
- Identify an existing native service or earlier process before starting another
  instance; include the stop/handoff in the proposed startup action.

Do **not** start `supervisord` during preparation: the example uses
`autostart=true`, which immediately starts both programs.

`robert service install` does not universally install Supervisor. It selects
systemd on Linux with a usable user bus, Supervisor on other Linux hosts, and
launchd on macOS. Only the Supervisor backend includes Web; its generated Web
command currently enables writable remote access on `0.0.0.0:8765`. Prefer the
local Supervisor example above. If the user chooses the automatic service path,
preview with `robert service install --dry-run --output json`, inspect the
backend and content, then follow the backend-specific guide before starting.

### 9. Confirm Before Starting

Explain which repository and GitHub events Robert will monitor, that accepted
tasks can publish authorized GitHub actions, the chosen Web access mode, and
any handoff from an existing service. Ask the user for explicit confirmation before starting unattended operation.

Only after confirmation, stop the identified earlier instance if applicable,
then start Supervisor:

```bash
supervisord -c ~/.config/robert/supervisord.conf
supervisorctl -c ~/.config/robert/supervisord.conf status
```

Wait past the configured `startsecs` and verify that both `robert-daemon` and
`robert-web` report `RUNNING`. Visit `http://127.0.0.1:8765/` and inspect
`/operations` and the two programs' logs for daemon activity or startup failures.
Use the actual executable paths resolved in step 8 if not available on PATH.

Starting live polling can execute queued work and write to GitHub. Do not create
remote test objects during setup or claim that the first cycle made no writes
without evidence. If the user has not confirmed startup, finish with Supervisor
prepared and both processes not started; do not report unattended operation as
verified.

## Final Report

Report:

- Robert version.
- Configuration path: `~/.config/robert/config.yml`.
- Runtime data path: `~/.local/share/robert/`.
- Repository and worker selected.
- Doctor result, Supervisor configuration path, both program statuses, Web URL,
  and access mode; distinguish prepared, started, and verified states.
- Every skipped, blocked, or failed check.

Preparation success means Robert is installed, configuration is valid, doctor
checks pass, Supervisor configuration is ready, and no GitHub write occurred during setup
verification. Report a skipped startup explicitly. Unattended-operation readiness
additionally requires user-confirmed startup and evidence from both programs and
daemon activity; report any GitHub effects observed after startup separately.

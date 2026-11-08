# Install Robert with a Coding Agent

[文档目录](README.md)

This guide is written for terminal coding agents such as Codex or Claude Code.
Read the entire guide before executing commands. This guide describes its own
Git revision, which can be newer than the PyPI release. Do not mix a release
package with main-branch configuration or instructions.

For human operators, use the [manual guide (中文)](human-install.md). This is
installation work, not permission to run a real GitHub task. Skills installed
in the host agent do not change the user's deployment authorization.

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
  other remote writes during setup preparation. An optional live acceptance
  task requires the explicit authorization described in step 10.
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
7. Install source: an existing release and its matching tag documentation, or
   the current source revision for this guide. Record the tag/full commit SHA.
8. Actual default branch, `upstream` target, writable `origin`, and repository
   commit identity. Use a dedicated checkout rather than one with unrelated work.
9. Any existing Robert service or Supervisor configuration that must be
   preserved or stopped before starting this instance.

The authenticated `gh` account, configured listening account, and trusted human
actor are different roles. Verify each; `github.account` does not switch gh login.
Use a distinct trusted human to trigger the first task: bot-authored mention comments do not trigger new work. Also identify the OS account that will run the service.

## Setup Procedure

### 1. Inspect the Environment

Detect Linux, macOS, or WSL. Check:

```bash
python3 --version
git --version
gh --version
pipx --version
```

Python must be an upstream-maintained stable release, currently 3.11–3.15. Also verify the selected worker command:

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

### 4. Select a revision and install Robert

For a published release, resolve an actual version from
[Releases](https://github.com/wklken/Robert/releases), replace `VERSION` below
with its package version (without the tag's leading `v`), and **continue using
that tag's guide**, which owns that release's configuration and commands:

```bash
pipx install robert-github-agent==VERSION --include-deps
```

For the capabilities in this guide, clone into a new user-approved source
directory, resolve one full commit SHA, and read the guide from that checkout.
Keep that revision for installation and verification; do not refetch main
between steps. For example, when `~/src/Robert` does not exist:

```bash
mkdir -p ~/src
git clone https://github.com/wklken/Robert.git ~/src/Robert
cd ~/src/Robert
git rev-parse HEAD
pipx install . --include-deps
```

This source checkout installs Robert; it is separate from the target repository
Robert will work on. Record the commit as well as `robert --version`, because
development commits can share the same version string.

`--include-deps` exposes `supervisord` and `supervisorctl` from Robert's
Supervisor dependency. If already installed, report the current version and ask
before upgrading. If those commands are not exposed, locate Robert's environment
with `pipx list` and use its `bin/supervisord` and `bin/supervisorctl` directly;
do not reinstall just to obtain PATH entries.

```bash
robert --version
robert --help
```

### 5. Verify the target repository and inspect existing configuration

Using the confirmed target repository and checkout, inspect:

```bash
gh api user --jq .login
gh repo view OWNER/REPO --json nameWithOwner,defaultBranchRef,viewerPermission
git -C /absolute/repository/path status --short
git -C /absolute/repository/path remote -v
git -C /absolute/repository/path var GIT_AUTHOR_IDENT
```

Current workspace preparation fetches from a remote named `upstream`.
It must point to the configured target repository. `origin` must point to the
branch repository the bot can write to (the same repository or its fork).
Inspect existing remotes before changing anything. If `upstream` is missing,
propose its exact URL and add it within the user's setup authorization; do not
replace an existing remote. Verify `git fetch upstream ACTUAL_DEFAULT_BRANCH`
in the checkout. This is a read, not proof that push is permitted. Git transport
authentication and `gh auth status` are separate checks. For HTTPS, if Git
should reuse gh authentication, explain the credential-helper change before
running `gh auth setup-git --hostname github.com` within the user's setup
authorization; preserve an existing credential setup. SSH uses its own keys. Resolve missing local
Git name/email with the user; do not invent a commit identity.

Do not reset, clean, or stash a dirty target checkout. Use a separate checkout
when authorized or report the missing preparation. Then inspect the config:

```bash
if test -f ~/.config/robert/config.yml; then
  echo "Configuration exists; inspect it before editing."
else
  echo "No configuration yet; proceed to initialization."
fi
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
  --repo-path "/absolute/repository/path" \
  --worker "WORKER" \
  --github-account ROBERT_ACCOUNT \
  --trusted-actor TRUSTED_ACTOR
```

Use the values confirmed by the user. Read the generated YAML and compare it
with the [configuration reference](reference.md#configuration):

- `github.account` is the listening account, while `repos[].trusted_actors`
  lists humans permitted to trigger work. Set the real `default_branch`; initialization always writes `main`.
- `workers` is a named mapping. Verify `adapter`, executable `command`, model,
  and effort. `init --worker` only auto-selects `codex`, `tcodex`, or `cbc`;
  for Claude, OpenCode, custom commands, and absolute executable paths, verify and correct the adapter explicitly.
- `routes` selects workers, skills and ordinary automatic model/effort overrides.
  Set the required `default_worker` name explicitly; native model participants
  use their own worker settings.
- Only `version: 2` is accepted. SQLite is the only execution engine; no engine block exists.
- Keep GitHub polling in `github`, capacity in `scheduler`, timeouts in
  `supervision`, and retention in `maintenance`. Enable repository PR automation
  only when requested; CI requires a nonempty check_allowlist.
- Keep `data_dir` consistent between daemon and web. Web options are command
  flags, not YAML keys. Do not copy internal normalized validation output back
  into the public configuration.

For an existing file, propose any necessary edits and obtain approval before
applying them. `robert init --force` replaces the file; it does not preserve custom settings.

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
writes local run records, but alone does not prove live GitHub polling or worker
execution. Doctor checks the default worker executable and gh authentication;
it does not verify all configured models, Git remotes, push access, or a real
model invocation. Check every selected worker command and its existing login
without printing credentials. Do not label skipped external checks as verified.

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
launchd on macOS. The default target `all` installs daemon and independent task
supervision. Only the Supervisor backend includes Web; its generated Web command
defaults to read-only access on `127.0.0.1:8765`. Reinstallation overwrites the
selected definitions. If the user chooses the automatic service path,
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

### 10. Optional first-task verification

After startup, offer the [first-task walkthrough (中文)](first-task.md). Only
perform it when the user explicitly authorizes the target repository and live
GitHub write. Prefer one analysis-only request from the trusted human first;
verify the task record, publication receipt, and actual comment. PR creation
needs a separate authorized implementation request and validates additional
Git push permissions. Do not create remote test objects merely to claim setup
success. Record this as pending if it was not authorized.

Default startup can wait `github.polling.fallback_seconds` (300 seconds) before
its first scan; check `/operations` before diagnosing an empty board as broken.
The manual Supervisor example manages daemon and Web; `robert supervisor` is
an additional independent observer, not the external Supervisor process manager.
Host reboot startup is the host service manager's responsibility.

## Final Report

Report:

- Robert version, installation source, and full source commit SHA or release tag.
- Configuration path: `~/.config/robert/config.yml`.
- Runtime data path: `~/.local/share/robert/`.
- Repository and worker selected.
- Doctor result, Supervisor configuration path, both program statuses, Web URL,
  and access mode; distinguish prepared, started, and verified states.
- Configured listening account, actual gh account, trusted human, default branch, and verified Git remotes (without credentials).
- First-task verification status and observed GitHub result, if authorized.
- Every skipped, blocked, or failed check.

Preparation success means Robert is installed, configuration is valid, doctor
checks pass, Supervisor configuration is ready, and no GitHub write occurred during setup
verification. Report a skipped startup explicitly. Unattended-operation readiness
additionally requires user-confirmed startup and evidence from both programs and
daemon activity; report any GitHub effects observed after startup separately.

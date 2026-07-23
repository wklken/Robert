# Development

## Setup

```bash
git clone https://github.com/wklken/Robert.git
cd Robert
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
python3 -B -m unittest discover -s tests
```

Use temporary paths while developing:

```bash
ROBERT_CONFIG=/tmp/robert-config.yml \
ROBERT_DATA_DIR=/tmp/robert-data \
robert init
```

Production defaults remain `~/.config/robert/config.yml` and
`~/.local/share/robert/`.

## Testing

Run focused tests first, then the complete gate:

```bash
python3 -B -m unittest tests.test_cli
python3 -B -m unittest discover -s tests
python3 -B -m compileall -q src
python3 -m build
git diff --check
```

Install the wheel into a clean environment:

```bash
python3 -m venv /tmp/robert-smoke
/tmp/robert-smoke/bin/pip install dist/*.whl
/tmp/robert-smoke/bin/robert --version
```

Tests use isolated temporary configuration and data directories instead of
production paths.

## Architecture Decisions

### Local and self-hosted

Robert uses local workers, Git, SQLite, and the authenticated `gh` CLI. This
keeps repository content and credentials under operator control.

### Polling for the first beta

Polling avoids GitHub App installation and webhook infrastructure. The tradeoff
is higher latency and GitHub API usage.

### Durable workstreams

SQLite records events, task ownership, attempts, results, actions, and
deduplication evidence. Restarting a daemon must not duplicate work.

### Route policy is partly immutable

Operators may choose workers and skills, but configuration cannot widen GitHub
actions, verification rules, output contracts, or workspace policy.

### Native user services

Unattended execution uses systemd user services or launchd. Shell PID
supervisors are not part of the standalone package.

### Read-only integrations

The web UI is read-only by default, and OpenClaw exposes read-only commands.
Writable local controls require explicit startup flags and CSRF protection.

## Releasing

Release preparation:

```bash
python3 -m pip install -e '.[dev]'
python3 -B -m unittest discover -s tests
python3 -B -m compileall -q src
python3 -m build
python3 -m twine check dist/*
git diff --check
```

Verify a clean wheel:

```bash
python3 -m venv /tmp/robert-release-smoke
/tmp/robert-release-smoke/bin/pip install dist/*.whl
/tmp/robert-release-smoke/bin/robert --version
```

The version is declared in `pyproject.toml` and `robert_agent.__version__`.
Release tags use `v<version>`. The GitHub release workflow publishes through
PyPI Trusted Publishing using repository `wklken/Robert`, workflow
`release.yml`, and environment `pypi`.

Never tag or publish until release evidence, secret scans, TestPyPI rehearsal,
and explicit publication approval are complete.

## Robert 0.1.0b1 Release Evidence

## Status

- Version: `0.1.0b1`
- Evidence date: `2026-07-23`
- Repository root: sanitized local checkout
- Public history policy: one sanitized release root plus evidence updates
- Publication status: completed
- GitHub writes performed by these gates: dedicated canary issue/comment,
  sanitized repository publication, release tag, and release evidence

## Environment

- Python: `3.10.16`
- Installed Codex CLI used by live worker acceptance: `0.145.0`
- GitHub CLI authentication: active account `wklken`
- OpenClaw CLI: not installed locally
- CI-equivalent Python environment: `/tmp/robert-dev-check`

The shared host Python installation has unrelated legacy `kube-shell`
dependency conflicts. All Robert release checks, including `pip check`, were
run in the isolated release environment, where they passed.

## Full Local Gate

Commands:

```bash
/tmp/robert-dev-check/bin/pip install -e '.[dev]'
/tmp/robert-dev-check/bin/python -m ruff check src tests
/tmp/robert-dev-check/bin/python -m mypy \
  src/robert_agent/cli \
  src/robert_agent/paths.py \
  src/robert_agent/route_config.py \
  src/robert_agent/skills.py \
  src/robert_agent/service.py \
  src/robert_agent/migrate.py
/tmp/robert-dev-check/bin/python -B -m unittest discover -s tests
/tmp/robert-dev-check/bin/python -B -m compileall -q src
/tmp/robert-dev-check/bin/python -m build
/tmp/robert-dev-check/bin/python -m pip_audit
git diff --check
```

Results:

- Ruff: passed
- Mypy: passed for 9 configured source targets
- Unit and controlled integration tests: `538` passed
- Compileall: passed
- Wheel and source distribution build: passed
- Dependency audit: no known vulnerabilities
- Diff whitespace check: passed

## Private-Reference and Secret Scan

The current tree and every reachable Git blob were scanned for private
organization names, personal paths, internal domains, credential headers,
private-key markers, token formats, and environment-specific checkout paths.

Result: no disallowed matches.

Legacy `dd-github-agent` strings remain only in migration commands, migration
documentation, migration implementation, and migration tests.

## Distribution Artifacts

| Artifact | SHA-256 |
| --- | --- |
| `robert_github_agent-0.1.0b1-py3-none-any.whl` | `1f502103f682b3430be7d66fc3a8c1883456cb9c511226dc62f65888958bffce` |
| `robert_github_agent-0.1.0b1.tar.gz` | `c4286a8211a8335d69e2133408a312778ad30af78f6ce10ee77dc44cf3dbd631` |

These hashes match the files published by the release workflow.

Packaging acceptance:

```bash
python3 -m venv /tmp/robert-final-smoke
/tmp/robert-final-smoke/bin/pip install \
  dist/robert_github_agent-0.1.0b1-py3-none-any.whl
/tmp/robert-final-smoke/bin/robert --help
/tmp/robert-final-smoke/bin/robert init --help
/tmp/robert-final-smoke/bin/robert doctor --help
/tmp/robert-final-smoke/bin/robert daemon run --help
/tmp/robert-final-smoke/bin/robert service install --dry-run
/tmp/robert-final-smoke/bin/robert web run --help
ROBERT_DATA_DIR=/tmp/robert-final-data \
  /tmp/robert-final-smoke/bin/robert openclaw install --dry-run
```

Result: every command exited successfully.

## Controlled Acceptance Evidence

### Controlled Issue-to-PR

- Timestamp: `2026-07-23 11:27:37 +08:00`
- Command:

```bash
/tmp/robert-final-smoke/bin/python \
  -m robert_agent.controlled_e2e_acceptance \
  --config /tmp/robert-acceptance-config.yml \
  --workspace-dir /tmp/robert-acceptance-controlled \
  --timeout-seconds 60 \
  --poll-interval-seconds 0.2
```

- Result: passed
- Route: `new-pr`
- Task lifecycle: `completed`
- Planned actions were accepted and marked published by the controlled fake
  publisher. No real GitHub command was executed.
- Evidence SHA-256:
  `98549cf670133a2983fbc4b586cf769186782b0479ebcf7231c1180051a6ba1a`

### Live Read-Only Discovery

- Timestamp: `2026-07-23 11:28:31 +08:00`
- Command:

```bash
/tmp/robert-final-smoke/bin/python \
  -m robert_agent.live_discovery_acceptance \
  --config /tmp/robert-acceptance-config.yml \
  --limit 10
```

- Result: passed
- Repository: `wklken/Robert`
- Read-only: `true`
- Raw events: `0`
- Normalized events: `0`
- Evidence SHA-256:
  `168554b1897d40419e4fae3eafe30341454e4281b51db4f9068c08f8572b0fe5`

### Isolated Worktree

- Timestamp: `2026-07-23 11:28:32 +08:00`
- Command:

```bash
/tmp/robert-final-smoke/bin/python \
  -m robert_agent.live_worktree_acceptance \
  --config /tmp/robert-acceptance-config.yml \
  --workspace-dir /tmp/robert-acceptance-worktree
```

- Result: passed
- Route: `new-pr`
- Worker attempt reached `running`
- Git branch:
  `codex/dd-77-fix-worktree-acceptance`
- Git worktree registration: verified
- Worker process cleanup: completed
- Evidence SHA-256:
  `a2b24846bb336060cd130c8a05ca0122a021abda485480489d79d0f98c9cb64b`

### Real Configured Worker Without GitHub Writes

- Timestamp: `2026-07-23 11:33:24 +08:00`
- Command:

```bash
/tmp/robert-final-smoke/bin/python \
  -m robert_agent.live_worker_acceptance \
  --config /tmp/robert-acceptance-config.yml \
  --workspace-dir /tmp/robert-acceptance-live-worker \
  --timeout-seconds 600 \
  --poll-interval-seconds 2
```

- Result: passed
- Worker: installed Codex CLI
- Route: `comment-analysis`
- Worker result: accepted
- Planned comment actions: `1`
- Published actions: `0`
- Publication mode: `dry_run`
- Evidence SHA-256:
  `ad7178b5b8c261bbc78e5546005103242e603db0b2bfa4cb556359f89d72cf41`

The first attempt exposed an unsupported Codex flag. The adapter was corrected,
a regression test was added, and the acceptance passed on rerun.

### Publication Deduplication

- Timestamp: `2026-07-23 11:28:32 +08:00`
- Command:

```bash
/tmp/robert-final-smoke/bin/python \
  -m robert_agent.publish_dedupe_acceptance \
  --workspace-dir /tmp/robert-acceptance-dedupe
```

- Result: passed
- Existing actions recognized: `2`
- Deduplicated actions: `2`
- Create commands issued: `0`
- Evidence SHA-256:
  `bc4b450dfc0b07fbdc7f0a3b68c7481809d76625c4b310ab5a02a10a9ae4b0c4`

### Daemon Poll-Starvation

- Timestamp: `2026-07-23 11:37:48 +08:00`
- Command:

```bash
/tmp/robert-dev-check/bin/python -B -m unittest \
  tests.test_daemon.DaemonSchedulingTests.test_run_once_decision_startup_live_poll_runs_at_full_capacity \
  tests.test_daemon.DaemonSchedulingTests.test_run_once_decision_due_live_poll_is_not_starved_by_running_attempt
```

- Result: `2` tests passed
- Evidence SHA-256:
  `a9fd3e4dfe661f02441e983076c3cd7cb2d889504e28b5a39da9b391abe63d2e`

## Asset and License Review

Bundled non-Python web assets:

- `board.css`
- `board.html`
- `board.js`
- `github-shell.css`
- `index.html`
- `operations.css`
- `workbench.css`
- `workbench.js`

These files are Robert project source, contain no vendored JavaScript or CSS
library, and are distributed under the repository's Apache-2.0 license. The
inline favicon is project-authored SVG markup.

Runtime dependency:

| Dependency | Reviewed version | Source | License |
| --- | --- | --- | --- |
| PyYAML | `6.0.3` | `pyyaml.org` / `github.com/yaml/pyyaml` | MIT |

PyYAML is installed as a dependency and is not copied into Robert's wheel.

`NOTICE` is not required. No reviewed bundled asset or runtime dependency adds
an Apache NOTICE obligation.

## Public Tree and Dead-Code Review

Commands:

```bash
git status --short
git log --oneline --decorate --reverse
rg -n \
  'daemon_supervisor|start_daemon\.sh|start_web\.sh|dd_status|dd_agent|dd-github-worker' \
  src tests docs README.md README_ZH.md
/tmp/robert-dev-check/bin/pip check
```

Results:

- No shell supervisor or legacy start script remains.
- No deleted worker package or old status module remains.
- Moved OpenClaw and status symbols have production CLI call sites.
- No proxy-only compatibility module remains.
- Isolated `pip check`: passed.
- Shared host `pip check`: unrelated pre-existing `kube-shell` conflicts only.
- Repository diff contains no unrelated feature work identified by the audit.

The dead-code and proxy-only scan was completed.

## OpenClaw Verification Limitation

OpenClaw is not installed on this host, so real plugin installation, runtime
inspection, and gateway restart were not executed. Current official OpenClaw
documentation was checked for:

- local-directory `plugins install`;
- `plugins inspect <id> --runtime --json`;
- `plugins uninstall <id> --force`;
- `gateway restart`.

Generated plugin tests, dry-run command generation, scheduler-content scans,
and `node --check` passed.

## Published Release

- Public repository:
  [wklken/Robert](https://github.com/wklken/Robert)
- Release:
  [v0.1.0b1](https://github.com/wklken/Robert/releases/tag/v0.1.0b1)
- PyPI:
  [robert-github-agent 0.1.0b1](https://pypi.org/project/robert-github-agent/0.1.0b1/)
- Release workflow:
  [GitHub Actions run 29985280105](https://github.com/wklken/Robert/actions/runs/29985280105)
- Production canary:
  [issue 6](https://github.com/wklken/Robert/issues/6)

The release workflow completed through PyPI Trusted Publishing, generated
provenance attestations for the wheel and source distribution, and created the
GitHub release.

The production canary published one marker-protected comment. A second run
deduplicated the action, and the issue contains exactly one canary marker.

TestPyPI was intentionally skipped by operator choice.

Public install verification:

```bash
python3 -m venv /tmp/robert-pypi-smoke
/tmp/robert-pypi-smoke/bin/pip install --upgrade pip
/tmp/robert-pypi-smoke/bin/pip install \
  --index-url https://pypi.org/simple/ \
  robert-github-agent==0.1.0b1
/tmp/robert-pypi-smoke/bin/robert --version
/tmp/robert-pypi-smoke/bin/robert --help
```

Result: `robert 0.1.0b1`.

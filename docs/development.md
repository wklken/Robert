# Development

[文档目录](README.md)

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
The Ruff rules and import-linter layer contracts are defined in `pyproject.toml`.
Install the development extra to run both. Import-linter is a development-only
dependency; its bounded version keeps the same contract engine on Python 3.10–3.14.
The architecture tests additionally reject cycles across the complete module graph.

```bash
python3 -B -m unittest tests.interfaces.cli.test_cli
python3 -m ruff check src tests
python3 -m mypy src/robert_agent/cli src/robert_agent/paths.py \
  src/robert_agent/configuration src/robert_agent/execution/stages.py \
  src/robert_agent/context/skills.py src/robert_agent/adapters/services/native.py
lint-imports --config pyproject.toml --no-cache
python3 -B -m unittest discover -s tests
python3 -B -m compileall -q src
python3 -m build
node --test tests/browser/*.test.cjs
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

Tests follow the owning domains under `tests/work/`, `tests/execution/`,
`tests/delivery/`, `tests/supervision/` and the other source boundaries. Adapter
tests live under the corresponding `tests/adapters/` protocol directory.
Repository-cycle scenarios stay under `tests/integration/repository_cycle/`;
real-process simulations and browser tests have their own directories. Shared
fixtures live in `tests/support/` and do not contain test methods. Each test
module should focus on one target or one integration behavior.

See [test rules](../tests/AGENTS.md) before adding fixtures or changing discovery.
Plans, reviews and remediation records under `docs/superpowers/`, `docs/reviews/`
and `docs/remediation/` are local archives ignored by Git; durable usage and
architecture rules belong in the published guides.

## Architecture contracts

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

Unattended execution supports Supervisor, systemd user services and launchd.
Daemon and independent task supervision have separate service targets. The
Supervisor daemon target also includes Web. See [service setup](guides.md#daemon-service).

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

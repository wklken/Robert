# Development

[文档目录](README.md)

## Setup

Use an upstream-maintained stable Python release. As of 2026-11-07, Robert
supports Python 3.11–3.15, including branches receiving security fixes only.
Python 3.10 reached end of life on 2026-10-01 and is no longer supported.
See the [official Python version status](https://devguide.python.org/versions/).

When a branch reaches EOL, raise `project.requires-python` in `pyproject.toml`
and update its classifiers, Ruff target, CI matrices and installation guides
together. Add new stable Python releases to the matrix before claiming support.
Also keep `main`'s required status checks aligned with the pull-request jobs:
`quality`, `secret-scan`, and the minimum/maximum Python test jobs. Requiring
release-only jobs or removed matrix entries leaves pull requests waiting for
checks that will never run.

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
dependency; its bounded version keeps the same contract engine across the CI matrix.
The architecture tests additionally reject cycles across the complete module graph.

Ruff is also the Python formatter. Its version is pinned in the development
extra so local formatting and CI agree. Use the stable style with a 120-column
line length, double quotes and spaces; Markdown prompt fixtures are excluded.
Apply formatting with `python3 -m ruff format src tests`, then run the checks below. CI rejects
formatting drift without modifying files.

```bash
python3 -B -m unittest tests.interfaces.cli.test_cli
python3 -m ruff check src tests
python3 -m ruff format --check src tests
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
tests live under the corresponding `tests/adapters/` protocol directory. Model
and database contracts live in `tests/models/` and `tests/persistence/`.
Repository-cycle scenarios stay under `tests/integration/repository_cycle/`;
real-process simulations and browser tests have their own directories. Shared
fixtures live in `tests/support/` and do not contain test methods. Each test
module should focus on one target or one integration behavior.

See [test rules](../tests/AGENTS.md) before adding fixtures or changing discovery.
Plans, reviews and remediation records under `docs/superpowers/`, `docs/reviews/`
and `docs/remediation/` are local archives ignored by Git; durable usage and
architecture rules belong in the published guides.

To exercise the installed wheel against offline GitHub and scripted Worker
fixtures, run the [Compose lifecycle suite](features/compose-integration-tests.md).
The standard unittest discovery stays Docker-free.

## CI checks

Pull requests and pushes to `main` run four jobs:

- The complete unittest suite on Linux with Python 3.11 and 3.15, the minimum
  and maximum supported versions.
- One Python 3.15 quality job for Ruff lint/format, import boundaries, the
  configured mypy targets, compilation and diff checks.
- Secret scanning.

New commits to a pull request cancel its superseded CI run. Ordinary CI does
not build packages, run installed-wheel smoke tests, audit dependencies or
check documentation links.

Pushing a `v*` release tag calls the same CI workflow with `full-checks: true`.
It expands the test matrix to Linux/macOS and every supported Python version
(3.11–3.15), audits each tested environment, and enables the installed-wheel
smoke and documentation-link jobs. Quality and secret checks also run.
The release publishing job waits for all these checks to pass.

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

The web UI and OpenClaw queries are read-only by default. Explicitly configured
[operator messaging](features/operator-messaging.md) enables scoped chat responses.
Writable local controls require explicit startup flags and CSRF protection.

## Releasing

Release preparation:

```bash
python3 -m pip install -e '.[dev]'
python3 -m ruff check src tests
python3 -m ruff format --check src tests
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

## Persistence dependency

SQLAlchemy 2 supplies declarative table mappings, parameter binding, composable predicates,
and atomic conditional updates through one persistence layer. The standard-library sqlite3
module and the existing dependencies do not provide these mapping and query-building
capabilities. The synchronous API supports the project's supported Python matrix; the
runtime keeps sqlite3's existing transaction behavior and uses no ORM session identity map.
The dependency range stays on the maintained 2.0 line. Its MIT license is compatible with
this project; release CI dependency audits include SQLAlchemy and its resolved dependencies.
No additional database service, migration tool, or asynchronous driver is required.

Hot read paths reuse immutable parameterized statement templates. A process-local cache
retains at most eight engines, keyed by database path and connection options, to reuse
SQLAlchemy compilation. Each engine uses `NullPool`: every `connect()` still opens a fresh
physical connection and owns its transaction. Neither rows nor active connections are cached.

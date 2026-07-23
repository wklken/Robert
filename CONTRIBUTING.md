# Contributing to Robert

Robert accepts issues and pull requests from the community. Keep changes
focused, preserve the trust and audit boundaries, and add behavior tests for
new logic.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

## Required verification

```bash
python3 -B -m unittest discover -s tests
python3 -m build
git diff --check
```

Add focused tests before the full suite. Documentation-only changes do not
require the Python suite unless they change executable examples or packaging.

## Dependencies and external actions

Explain every new dependency and why the standard library and current
dependencies are insufficient. Pull requests must disclose new network calls,
filesystem writes, subprocesses, credentials, or GitHub actions.

## DCO sign-off

Robert uses the Developer Certificate of Origin. Sign every commit:

```bash
git commit -s
```

By signing, you certify that you have the right to submit the contribution.
Robert does not require a contributor license agreement for the first beta.

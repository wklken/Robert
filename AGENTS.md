# AGENTS.md

## Scope

These rules apply to the entire Robert repository.

## Required behavior

- Read the target modules and tests before changing them.
- Keep changes limited to the active task.
- Add a failing behavior test before bug fixes.
- Preserve the trust, workstream, audit, redaction, and idempotency boundaries.
- Do not add runtime dependencies without documenting why the standard library
  and current dependencies are insufficient.
- Do not store GitHub or OpenClaw credentials in configuration or fixtures.
- Run `python3 -B -m unittest discover -s tests` for code changes.
- Run `python3 -m build` for packaging or resource changes.
- Run `git diff --check` before committing.
- Documentation-only changes do not require the Python test suite.

## Deployment checks

- During installation or upgrades, check the service account and configured
  workers. The `claude` and `tclaude` adapters request `bypassPermissions`,
  which Claude Code normally rejects when running as root.
- When root execution in an already restricted container is intended, prefix
  each affected worker's configured command with `env IS_SANDBOX=1`, keeping
  its adapter, executable path, and existing arguments. Follow the
  [container configuration guide](docs/guides.md#claude-workers-as-root-in-a-restricted-container).
  This variable does not create a sandbox or reduce root privileges; do not
  apply it automatically to an unrestricted host.
- Configuration validation, doctor, version checks, and mocked launches do not
  prove a real Claude worker can start. Verify an actual launch within the
  authorized deployment scope, or report that verification as pending.

## Delivery authority

Local changes and commits do not authorize pushing, opening pull requests,
publishing packages, creating releases, or writing to GitHub.

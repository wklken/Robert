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

## Delivery authority

Local changes and commits do not authorize pushing, opening pull requests,
publishing packages, creating releases, or writing to GitHub.

# Contributing to Robert

欢迎提交中文或英文 Issue、文档改进和 Pull Request。Thanks for helping improve Robert.
Start with a focused problem and a small, reviewable change.

## Choose where to contribute

- Installation or usage questions: [support channels](SUPPORT.md).
- Reproducible defects, documentation gaps, or feature proposals: [issue forms](https://github.com/wklken/Robert/issues/new/choose).
- Suspected vulnerabilities: [private reporting](SECURITY.md#reporting-a-vulnerability).
- Community expectations: [Code of Conduct](CODE_OF_CONDUCT.md).

For a substantial behavior or architecture change, explain the problem and
proposed scope in an issue first. Typo corrections and bounded fixes can go
straight to a PR. Useful first contributions include reproducing a bug,
clarifying a failed installation step, or adding a focused behavior test.

## Set up and verify

Follow the [development guide](docs/development.md) for the supported Python
versions, environment setup, and exact lint, type, import, test, browser, and
build commands. Read [AGENTS.md](AGENTS.md) and any rules beside the files you edit.
Use disposable configuration and data paths, not a running Robert instance.

- Read affected modules and tests before editing; add a failing behavior test before a bug fix.
- Keep trust, workstream ownership, audit, redaction, and idempotency checks intact.
- Update relevant usage documentation when behavior, configuration, or commands change.
- Run affected checks and required gates from the development guide. For prose-only changes, check links, examples, formatting, and `git diff --check`; the application suite is not required.
- Explain new dependencies and why the standard library or existing dependencies are insufficient.

## Submit a pull request

Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md). Describe the user-visible
problem, resulting behavior, and verification with commands and outcomes.
Mark inapplicable checks as N/A and disclose skipped or blocked checks.
Include reproduction steps for defects and screenshots for visible UI changes.
Disclose new network calls, filesystem writes, subprocesses, credentials, or GitHub actions.

Keep credentials, private repository content, diagnostic archives, and local
planning/review records out of commits. Do not mix unrelated formatting or
cleanup into a focused change. A contribution does not authorize deployment,
GitHub writes against a user's repositories, or package publication.

## License and sign-off

Robert is licensed under [Apache License 2.0](LICENSE). The current beta does
not require DCO sign-off or a contributor license agreement. Voluntary sign-off
is welcome (`git commit -s`).

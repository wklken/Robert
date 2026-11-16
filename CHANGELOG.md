# Changelog

## Unreleased

- Add TAPD @ requests as a work source: a configured trusted user's mention starts a request, a ticket label selects the repository, and results are answered on the ticket or delivered through a derived GitHub pull request. See [the TAPD guide](docs/features/tapd.md).
- Upgrading rebuilds the source tables for provider identities and first copies the database to `<database>.pre-source-providers`. Before any TAPD row is written, the previous release can still use the upgraded database. After TAPD is enabled, rolling back requires restoring that backup or another database backup.
- Recheck a publication whose result is unknown after transient read failures, using the retry budget; ambiguous or malformed evidence still requires the operator. This applies to GitHub and TAPD.

## 0.1.0b2

- Add a Supervisor fallback for Linux hosts without user systemd, including service status and control support.
- Improve OpenClaw plugin installation diagnostics and verify installed commands expose Robert capabilities.
- Nest daemon configuration settings in the generated config and schema.
- Fix GitHub Actions release, DCO, gitleaks, documentation-link, and worker-dispatch checks.
- Refresh installation documentation, README copy, and Web UI/OpenClaw screenshots.

## 0.1.0b1

- Initial public beta of Robert.
- Standalone Python package and `robert` CLI.
- Multi-repository trusted GitHub workstreams.
- Configurable worker adapters and route skills.
- Native user services, local web UI, and optional OpenClaw commands.

# Changelog

## Unreleased

## 0.1.0b3

### Breaking Changes

- Python 3.11 is now the minimum supported version.
- Robert now accepts only version 2 YAML configuration. Review the [configuration reference](docs/reference.md#configuration) before upgrading.

### Highlights

- Accept TAPD requests from configured trusted users. Ticket labels select the repository, and Robert can reply in TAPD or deliver through a derived GitHub PR. See the [TAPD guide](docs/features/tapd.md).
- Add a lifecycle board and operator commands for questions, approvals, retries, cancellation, and publication recovery. The board shows who needs to act and what they can do next. See the [lifecycle board guide](docs/features/web-lifecycle-board.md).
- Browse paginated Agent messages and actions from the board, with access limited to the selected execution and its model runs.
- Add bounded task supervision, operator messaging, and optional multi-model review before new PR publication. See the [supervision guide](docs/features/task-supervision.md) and [messaging guide](docs/features/operator-messaging.md).

### Improvements

- Opt in to CI and conflict remediation for managed PRs. Select observed component workflows by name or exact workflow path, including runs with dynamic names.
- Recheck unknown GitHub and TAPD publication outcomes after transient read failures within the retry budget.

### Fixes

- Resume delivery on its original worktree after a trusted verification reply, preserving pending edits and the original target.
- Show current PR remediation alerts on the board, and clear stale alerts when the PR revision changes or its checks turn green.
- Tolerate short SQLite writer contention, group repeated outage and CI alerts, and suppress notices for canceled work or alerts resolved before sending.
- Support configured symlink aliases for pre-publication review roots while rejecting symlinks within review artifact directories.

### Upgrade Guide

- Finish or reconcile in-flight work with the previous version. Stop Robert and back up both the SQLite database and the result spool. Prepare and validate the version 2 configuration against the new version before starting the service.
- Initialization rebuilds source tables that predate provider identities and first creates `<database>.pre-source-providers`. After TAPD writes data, rolling back requires restoring a compatible database backup. See the [database upgrade details](docs/reference.md#database).

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

# Architecture Decisions

## Local and self-hosted

Robert uses local workers, Git, SQLite, and the authenticated `gh` CLI. This
keeps repository content and credentials under operator control.

## Polling for the first beta

Polling avoids GitHub App installation and webhook infrastructure. The tradeoff
is higher latency and GitHub API usage.

## Durable workstreams

SQLite records events, task ownership, attempts, results, actions, and
deduplication evidence. Restarting a daemon must not duplicate work.

## Route policy is partly immutable

Operators may choose workers and skills, but configuration cannot widen GitHub
actions, verification rules, output contracts, or workspace policy.

## Native user services

Unattended execution uses systemd user services or launchd. Shell PID
supervisors are not part of the standalone package.

## Read-only integrations

The web UI is read-only by default, and OpenClaw exposes read-only commands.
Writable local controls require explicit startup flags and CSRF protection.

Configuration and runtime roots are `~/.config/robert/config.yml` and
`~/.local/share/robert/`.

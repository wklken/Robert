# Architecture

Robert is a local Python control plane with five boundaries:

1. discovery and authorization through the authenticated `gh` CLI;
2. routing and immutable workspace policy;
3. worker dispatch inside repository-specific Git worktrees;
4. result audit, redaction, and publication deduplication;
5. durable SQLite state under `~/.local/share/robert/`.

The installed `robert` command reads `~/.config/robert/config.yml`. A bounded
cycle validates configuration, acquires repository leases, supervises active
attempts, processes events, prepares tasks, dispatches workers, audits results,
publishes allowed actions, and records a summary.

Core modules remain intentionally direct: `run_once.py` orchestrates a cycle,
`daemon.py` schedules cycles, `storage.py` owns schema setup, and route,
worktree, dispatch, audit, and publish modules enforce separate policies.

Robert uses polling in the first beta. It does not include a webhook receiver or
GitHub App authentication.

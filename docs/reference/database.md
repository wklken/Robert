# Database Reference

The default database is
`~/.local/share/robert/robert.sqlite3`.

Major table groups:

- repositories, actors, and permissions;
- GitHub sources and normalized events;
- workstreams, tasks, attempts, and route decisions;
- artifacts, worker results, verification, and usage;
- planned GitHub actions and notifications;
- daemon runs, leases, wakeups, work items, and project memory.

Schema initialization is idempotent and applies guarded migrations. Do not edit
rows manually while Robert is running. Use read-only commands:

```bash
robert status --config ~/.config/robert/config.yml --output json
robert task show TASK_ID --config ~/.config/robert/config.yml --output json
```

Create a filesystem backup before offline maintenance. Diagnostics exports do
not include the raw database.

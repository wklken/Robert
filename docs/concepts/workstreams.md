# Workstreams

A workstream is the durable collaboration thread for one GitHub issue, pull
request, or local work item. It links sources, events, tasks, attempts,
artifacts, worker results, and publication actions.

Issue and pull-request mainlines remain separate. A Robert-created pull request
records its origin issue, while review follow-up stays on the pull-request
workstream and reuses its branch.

Only one active task owns a workstream at a time. Additional authorized events
become pending context. Waiting-for-user tasks resume only after an authorized
reply. Completed and failed work remains queryable:

```bash
robert status --config ~/.config/robert/config.yml
robert task show TASK_ID --config ~/.config/robert/config.yml --output json
```

The durable database is normally
`~/.local/share/robert/robert.sqlite3`.

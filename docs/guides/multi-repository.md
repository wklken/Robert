# Multiple Repositories

Add repositories to `~/.config/robert/config.yml` with distinct checkouts and
worktree roots:

```yaml
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/repos/backend/.worktrees
    default_branch: main
    trusted_actors: [maintainer]
  - full_name: example/frontend
    checkout: /srv/repos/frontend
    worktrees: /srv/repos/frontend/.worktrees
    default_branch: main
    trusted_actors: [maintainer]
```

Validate and inspect the shared state:

```bash
robert config validate --config ~/.config/robert/config.yml
robert status --config ~/.config/robert/config.yml --output json
```

All repository pipelines store durable evidence in
`~/.local/share/robert/`, but each worker receives the workspace belonging to
its task's repository.

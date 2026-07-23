# Configuration Reference

Default path: `~/.config/robert/config.yml`.

```yaml
version: 1
data_dir: ~/.local/share/robert
database: robert.sqlite3
github:
  account: robert-bot
  poll_seconds: 300
skills:
  search_paths: []
workers:
  default:
    adapter: codex
    command: codex
    model: default
    effort: default
    prompt_transport: stdin
    timeout_seconds: 5400
    environment_allowlist: []
routes: {}
repos:
  - full_name: example/backend
    checkout: /srv/repos/backend
    worktrees: /srv/repos/backend/.worktrees
    default_branch: main
    trusted_actors: [maintainer]
```

`github` accepts only `account` and `poll_seconds`; credentials are rejected.
Worker names preserve declaration order. The `command` adapter requires a YAML
sequence. Repository route overrides may set `worker`, `required_skills`, and
`recommended_skills` only.

Validate:

```bash
robert config validate --config ~/.config/robert/config.yml --output json
```

Runtime data is resolved below `~/.local/share/robert/` unless `data_dir` is
changed.

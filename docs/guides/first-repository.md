# First Repository

Create the initial configuration interactively:

```bash
robert init --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml --output json
```

Or use a non-interactive command:

```bash
robert init --non-interactive \
  --config ~/.config/robert/config.yml \
  --repo example/backend \
  --repo-path /srv/repos/backend \
  --worker codex \
  --github-account robert-bot \
  --trusted-actor maintainer
```

Robert stores the database and task artifacts under
`~/.local/share/robert/`. Preview one cycle before enabling the service:

```bash
robert run once --config ~/.config/robert/config.yml --dry-run --skip-external
```

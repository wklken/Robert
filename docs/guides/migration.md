# Migration

The legacy source defaults to `~/.agents/data/dd-github-agent`; the target is
`~/.local/share/robert/`.

Preview migration:

```bash
robert migrate dd-github-agent \
  --source ~/.agents/data/dd-github-agent \
  --target ~/.local/share/robert \
  --dry-run
```

Run migration:

```bash
robert migrate dd-github-agent \
  --source ~/.agents/data/dd-github-agent \
  --target ~/.local/share/robert
```

Robert creates a sibling backup before copying, converts the YAML shape,
renames the SQLite account column, and preserves lookup compatibility for
legacy idempotency markers. Review `~/.config/robert/config.yml` after migration
if you keep configuration separate from the runtime directory.

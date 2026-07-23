# OpenClaw

Generate and install the optional chat-only plugin:

```bash
robert openclaw install
robert openclaw status
```

Preview external commands while still generating the plugin files:

```bash
robert openclaw install \
  --plugin-dir ~/.local/share/robert/openclaw-plugin/robert-openclaw \
  --dry-run --output json
```

The plugin exposes `/robert-status`, `/robert-task`, `/robert-run`, and
`/robert-artifact`. It calls the installed `robert` CLI with JSON output. It
does not schedule Robert, create periodic jobs, or store gateway credentials in
`~/.config/robert/config.yml`.

Remove it with:

```bash
robert openclaw uninstall --dry-run
robert openclaw uninstall
```

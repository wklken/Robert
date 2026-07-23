# Worker Adapters

Worker definitions live in `~/.config/robert/config.yml`:

```yaml
workers:
  default:
    adapter: codex
    command: codex
    model: default
    effort: default
    prompt_transport: stdin
    timeout_seconds: 5400
    environment_allowlist: []
```

Built-in adapters are `codex`, `tcodex`, `cbc`, and `command`. The generic
`command` adapter requires a YAML command sequence:

```yaml
workers:
  custom:
    adapter: command
    command: [custom-agent, --batch]
    model: default
    effort: default
```

Check availability with:

```bash
robert doctor --config ~/.config/robert/config.yml --output json
```

Worker artifacts are registered under `~/.local/share/robert/`.

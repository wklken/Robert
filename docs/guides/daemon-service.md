# Daemon Service

Install and start the native user service:

```bash
robert service install --config ~/.config/robert/config.yml
robert service start
robert service status
```

Robert writes runtime data to `~/.local/share/robert/`. Linux uses
`~/.config/systemd/user/robert.service`; macOS uses
`~/Library/LaunchAgents/dev.robert.agent.plist`.

Preview without writing service files:

```bash
robert service install \
  --config ~/.config/robert/config.yml \
  --dry-run --output json
```

Use `robert service restart`, `robert service stop`, and
`robert service uninstall` for lifecycle management. For foreground debugging:

```bash
robert daemon run --config ~/.config/robert/config.yml
```

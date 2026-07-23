# Environment Variables

Robert recognizes:

| Variable | Purpose |
| --- | --- |
| `ROBERT_CONFIG` | Override `~/.config/robert/config.yml`. |
| `ROBERT_DATA_DIR` | Override `~/.local/share/robert/`. |

Worker subprocesses receive a fixed base set: `PATH`, `HOME`, `LANG`, `LC_ALL`,
`TMPDIR`, `SHELL`, and `USER`. Additional variable names must be listed in the
worker's `environment_allowlist`; values remain in the host environment and are
not stored in configuration.

Example:

```bash
ROBERT_CONFIG=/etc/robert/config.yml \
ROBERT_DATA_DIR=/var/lib/robert \
robert doctor --output json
```

Do not place GitHub tokens or OpenClaw credentials in
`~/.config/robert/config.yml`.

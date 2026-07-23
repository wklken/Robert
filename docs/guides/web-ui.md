# Local Web UI

Start the default read-only server:

```bash
robert web run --config ~/.config/robert/config.yml
```

Open `http://127.0.0.1:8765/`. The server reads the database under
`~/.local/share/robert/` and rejects operator commands.

Enable local writes explicitly:

```bash
robert web run \
  --config ~/.config/robert/config.yml \
  --writable \
  --operator "$USER"
```

Remote binding requires both `--allow-remote` and an authenticated reverse
proxy. Writable requests require `X-Robert-CSRF-Token`. Artifact previews read
only exact paths registered in SQLite and enforce a byte limit.

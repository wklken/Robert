# CLI Reference

Global:

```bash
robert --help
robert --version
```

Configuration and readiness:

```bash
robert init --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml --output json
robert config validate --config ~/.config/robert/config.yml
robert config show --config ~/.config/robert/config.yml --output json
robert config path
```

Cycles and inspection:

```bash
robert run once --config ~/.config/robert/config.yml
robert run show RUN_ID --config ~/.config/robert/config.yml --output json
robert status --config ~/.config/robert/config.yml
robert task show TASK_ID --config ~/.config/robert/config.yml
robert artifact show TASK_ID ARTIFACT_TYPE --config ~/.config/robert/config.yml
```

Operations:

```bash
robert daemon run --config ~/.config/robert/config.yml
robert service install --config ~/.config/robert/config.yml
robert service start
robert web run --config ~/.config/robert/config.yml
robert diagnostics export --output robert-diagnostics.zip
```

Integrations and migration:

```bash
robert openclaw install
robert openclaw status
robert migrate dd-github-agent --dry-run
```

The normal runtime directory is `~/.local/share/robert/`.

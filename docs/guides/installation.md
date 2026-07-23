# Installation

Install Robert with `pipx`:

```bash
pipx install robert-github-agent
gh auth login
robert --version
robert init --config ~/.config/robert/config.yml
robert doctor --config ~/.config/robert/config.yml
```

Runtime state is written to `~/.local/share/robert/`. Linux and macOS are
supported directly. On Windows, install and run Robert inside WSL.

For development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

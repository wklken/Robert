# Development Setup

```bash
git clone https://github.com/wklken/robert.git
cd robert
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
python3 -B -m unittest discover -s tests
```

Use a temporary configuration while developing:

```bash
ROBERT_CONFIG=/tmp/robert-config.yml \
ROBERT_DATA_DIR=/tmp/robert-data \
robert init
```

The production defaults remain `~/.config/robert/config.yml` and
`~/.local/share/robert/`.

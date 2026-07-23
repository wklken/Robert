# Testing

Run focused tests first, then the complete gate:

```bash
python3 -B -m unittest tests.test_cli
python3 -B -m unittest discover -s tests
python3 -B -m compileall -q src
python3 -m build
git diff --check
```

Install the wheel into a clean environment:

```bash
python3 -m venv /tmp/robert-smoke
/tmp/robert-smoke/bin/pip install dist/*.whl
/tmp/robert-smoke/bin/robert --version
```

Tests use isolated temporary configuration and data directories instead of
`~/.config/robert/config.yml` and `~/.local/share/robert/`.

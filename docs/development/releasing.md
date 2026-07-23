# Releasing

Release preparation:

```bash
python3 -m pip install -e '.[dev]'
python3 -B -m unittest discover -s tests
python3 -B -m compileall -q src
python3 -m build
python3 -m twine check dist/*
git diff --check
```

Verify a clean wheel:

```bash
python3 -m venv /tmp/robert-release-smoke
/tmp/robert-release-smoke/bin/pip install dist/*.whl
/tmp/robert-release-smoke/bin/robert --version
```

The version is declared in `pyproject.toml` and `robert_agent.__version__`.
Release tags use `v<version>`. The GitHub release workflow publishes through
PyPI Trusted Publishing using repository `wklken/robert`, workflow
`release.yml`, and environment `pypi`.

Never tag or publish until release evidence, secret scans, TestPyPI rehearsal,
and explicit publication approval are complete. Local configuration and data
remain at `~/.config/robert/config.yml` and `~/.local/share/robert/`.

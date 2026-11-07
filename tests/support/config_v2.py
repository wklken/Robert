"""Shared fixtures for config v2 tests."""

from pathlib import Path
import tempfile
import unittest
import yaml
from robert_agent.configuration.command import validate_config


class ConfigV2Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "repo" / ".git").mkdir(parents=True)
        self.path = self.root / "config.yml"
        self.config = {
            "version": 2,
            "data_dir": str(self.root / "data"),
            "database": "robert.sqlite3",
            "github": {"account": "bot"},
            "default_worker": "b",
            "workers": {
                "a": {
                    "adapter": "codex",
                    "command": "codex",
                    "model": "model-a",
                    "effort": "high",
                },
                "b": {
                    "adapter": "claude",
                    "command": "claude",
                    "model": "model-b",
                    "effort": "low",
                },
            },
            "repos": [
                {
                    "full_name": "example/test",
                    "checkout": str(self.root / "repo"),
                    "worktrees": str(self.root / "trees"),
                    "default_branch": "main",
                    "trusted_actors": ["maintainer"],
                }
            ],
        }

    def validate(self, config=None):
        self.path.write_text(yaml.safe_dump(self.config if config is None else config, sort_keys=False))
        return validate_config(self.path, skip_external=True)

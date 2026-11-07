"""Shared fixtures for multi review config tests."""

from pathlib import Path
import tempfile
import unittest
import yaml
from robert_agent.configuration import command as validate_config


class ReviewConcurrencyConfigFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        checkout = self.root / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        self.path = self.root / "config.yml"
        self.config = {
            "version": 2,
            "default_worker": "codex",
            "data_dir": str(self.root / "data"),
            "database": "robert.sqlite3",
            "github": {"account": "robert-bot"},
            "scheduler": {"max_concurrency": 1},
            "workers": {
                "codex": {
                    "adapter": "codex",
                    "command": "codex",
                    "model": "default",
                    "effort": "default",
                }
            },
            "repos": [
                {
                    "full_name": "example/backend",
                    "checkout": str(checkout),
                    "worktrees": str(self.root / "worktrees"),
                    "default_branch": "main",
                    "trusted_actors": ["maintainer"],
                }
            ],
        }

    def validate(self, config):
        self.path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return validate_config.validate_config(self.path, skip_external=True)

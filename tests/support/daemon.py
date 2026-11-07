"""Shared daemon database and scheduling inputs."""
import tempfile
import unittest
from pathlib import Path


class DaemonFixture(unittest.TestCase):
    def setUp(self):
        from robert_agent.runtime import daemon

        # The stop flag is module state; it now also gates child launches.
        self.addCleanup(setattr, daemon, "_STOP_REQUESTED", False)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "dd.sqlite3"

    def _init_db(self):
        from robert_agent.adapters.sqlite import database as storage

        storage.init_database(self.db_path)
        return self.db_path

    def _insert_repo(self, conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO repos(
              repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root
            )
            VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/repo/.worktrees')
            """
        )
        return "repo-1"

    def _write_config(self, overrides=None):
        import yaml
        from robert_agent.operations.initialize import init_config
        repo_root = self.root / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        config_path = self.root / "config.yml"
        init_config(config_path, {"repo": "example/repo", "repo_path": str(repo_root),
                    "worker": "cbc", "github_account": "robot", "trusted_actor": "wklken"})
        config = yaml.safe_load(config_path.read_text())
        config.update(data_dir=str(self.root), database="dd.sqlite3")
        config.update(overrides or {})
        config_path.write_text(yaml.safe_dump(config))
        return config_path

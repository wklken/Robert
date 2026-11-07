from tests.support.temp_workspace import TemporaryWorkspace


class AcceptanceFixture(TemporaryWorkspace):
    def _write_acceptance_config(self):
        repo_root = self.root / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        config_path = self.root / "config.yml"
        config_path.write_text(
            f"""version: 2
data_dir: {self.root / 'data'}
database: dd.sqlite3
github:
  account: robot
default_worker: default
workers:
  default:
    adapter: cbc
    command: cbc
    model: gpt-5.4
    effort: high
repos:
  - full_name: x/y
    github_account: robot
    trusted_actors:
    - wklken
    checkout: {repo_root}
    worktrees: {worktree_root}
    default_branch: master
""",
            encoding="utf-8",
        )
        return config_path

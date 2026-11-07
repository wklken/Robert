from tests.support.temp_workspace import TemporaryWorkspace


class InitializeTests(TemporaryWorkspace):
    def test_init_config_creates_public_config_and_refuses_overwrite(self):
        from robert_agent.operations import initialize as init_config
        target = self.root / "nested" / "config.yml"
        repo = self.root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        values = {
            "repo": "example/backend",
            "repo_path": str(repo),
            "worker": "codex",
            "github_account": "robert-bot",
            "trusted_actor": "maintainer",
        }

        created = init_config.init_config(
            target,
            values,
            non_interactive=True,
        )

        self.assertTrue(created["ok"], created)
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["config_path"], str(target))
        self.assertIn("full_name: example/backend", target.read_text(encoding="utf-8"))

        second = init_config.init_config(
            target,
            values,
            non_interactive=True,
        )

        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "exists")
        self.assertIn("already exists", second["safe_error"])

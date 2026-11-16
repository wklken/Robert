"""Local workspace and branch names come from a source reference, not a GitHub number."""

from pathlib import Path
import tempfile
import unittest

from robert_agent.execution import workspace


class WorkspaceNamingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.repo = {
            "repo_root": str(root / "repo"),
            "worktree_root": str(root / "worktrees"),
            "default_base_branch": "main",
        }

    def test_github_and_local_sources_keep_their_existing_reference(self):
        self.assertEqual(workspace.source_reference({"number": 123}), 123)
        self.assertEqual(workspace.source_reference({"provider": "github", "number": 123}), 123)
        self.assertEqual(workspace.source_reference({"origin_type": "web", "number": "3f2a9c1d0b4e"}), "3f2a9c1d0b4e")

    def test_tapd_sources_use_their_public_reference(self):
        event = {"provider": "tapd", "number": None, "public_ref": "rbt-7k2m9q4x"}
        self.assertEqual(workspace.source_reference(event), "rbt-7k2m9q4x")
        for public_ref in (None, "", "1100", "rbt-", "rbt-../main", "RBT-7K2M9Q4X"):
            with self.subTest(public_ref=public_ref), self.assertRaises(ValueError):
                workspace.source_reference({**event, "public_ref": public_ref})

    def test_new_branch_and_analysis_workspaces_are_named_by_reference(self):
        events = {
            "github": ({"number": 123, "title": "Fix login"}, "123"),
            "tapd": (
                {"provider": "tapd", "number": None, "public_ref": "rbt-7k2m9q4x", "title": "Fix login"},
                "rbt-7k2m9q4x",
            ),
        }
        for name, (event, reference) in events.items():
            with self.subTest(name):
                branch = workspace.prepare_worktree(self.repo, event, {"workspace_mode": "new_branch"}, dry_run=True)
                analysis = workspace.prepare_worktree(
                    self.repo, event, {"workspace_mode": "analysis", "enforce_workspace_policy": True}, dry_run=True
                )
                self.assertEqual(branch["branch_name"], f"codex/dd-{reference}-fix-login")
                self.assertEqual(Path(analysis["worktree_path"]).name, f"analysis-{reference}")

    def test_pull_request_workspaces_still_fetch_the_pull_request_number(self):
        event = {"number": 42, "title": "Review", "existing_pr_head_branch": "feature"}
        review = workspace.prepare_worktree(self.repo, event, {"workspace_mode": "review_pr"}, dry_run=True)
        existing = workspace.prepare_worktree(self.repo, event, {"workspace_mode": "existing_pr"}, dry_run=True)
        self.assertIn("pull/42/head", " ".join(review["commands"]))
        self.assertIn("pull/42/head", " ".join(existing["commands"]))

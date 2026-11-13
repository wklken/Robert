from pathlib import Path
from tests.support.temp_workspace import TemporaryWorkspace


class WorkspacePreparationTests(TemporaryWorkspace):
    def test_worktree_plans_new_branch_from_upstream_base(self):
        from robert_agent.adapters.git import workspace as worktree

        result = worktree.plan_worktree(
            repo_root=self.root / "repo",
            worktree_root=self.root / "repo" / ".worktrees",
            source_number=123,
            short_slug="fix-timeout",
            base_branch="master",
            dry_run=True,
        )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["branch_name"], "codex/dd-123-fix-timeout")
        self.assertEqual(result["start_point"], "upstream/master")
        self.assertIn("git fetch upstream", result["commands"][0])
        self.assertIn("git worktree add", result["commands"][1])

    def test_worktree_reuses_existing_pr_head_branch(self):
        from robert_agent.adapters.git import workspace as worktree

        result = worktree.plan_worktree(
            repo_root=self.root / "repo",
            worktree_root=self.root / "repo" / ".worktrees",
            source_number=456,
            short_slug="review-fix",
            base_branch="master",
            existing_pr_head_branch="codex/dd-123-fix-timeout",
            dry_run=True,
        )

        self.assertEqual(result["branch_name"], "codex/dd-123-fix-timeout")
        self.assertEqual(result["mode"], "reuse_existing_pr")
        self.assertEqual(
            result["commands"][0],
            "git fetch upstream pull/456/head:refs/heads/codex/dd-123-fix-timeout",
        )
        self.assertIn("git worktree add", result["commands"][1])

    def test_worktree_reuses_existing_worktree_when_branch_already_checked_out(self):
        from robert_agent.adapters.git import workspace as worktree

        repo_root = self.root / "repo"
        repo_root.mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()

        original_run = worktree.subprocess.run

        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(args, **kwargs):
            self.assertEqual(args, ["git", "worktree", "list", "--porcelain"])
            self.assertEqual(kwargs["cwd"], repo_root)
            return Completed(
                """worktree /tmp/repo\nHEAD abc\nbranch refs/heads/master\n\nworktree /tmp/repo/.worktrees/issue-2878-mcp-proxy\nHEAD def\nbranch refs/heads/codex/issue-2878-mcp-proxy\n\n"""
            )

        try:
            worktree.subprocess.run = fake_run
            result = worktree.plan_worktree(
                repo_root=repo_root,
                worktree_root=worktree_root,
                source_number=456,
                short_slug="review-fix",
                base_branch="master",
                existing_pr_head_branch="codex/issue-2878-mcp-proxy",
                dry_run=True,
            )
        finally:
            worktree.subprocess.run = original_run

        self.assertEqual(result["mode"], "reuse_existing_worktree")
        self.assertEqual(result["worktree_path"], "/tmp/repo/.worktrees/issue-2878-mcp-proxy")
        self.assertEqual(
            result["commands"],
            ["reuse existing worktree /tmp/repo/.worktrees/issue-2878-mcp-proxy"],
        )

    def test_review_worktree_fetches_pr_head(self):
        from robert_agent.adapters.git import workspace as worktree

        result = worktree.plan_review_worktree(
            repo_root=Path("/tmp/repo"),
            worktree_root=Path("/tmp/repo/.worktrees"),
            source_number=707,
            short_slug="Review Feature PR",
            base_branch="master",
            dry_run=True,
        )

        self.assertEqual(result["mode"], "review_pr")
        self.assertEqual(result["branch_name"], "review/pr-707-review-feature-pr")
        self.assertEqual(
            result["worktree_path"],
            "/tmp/repo/.worktrees/review__pr-707-review-feature-pr",
        )
        self.assertEqual(result["start_point"], "upstream/master")
        self.assertEqual(
            result["commands"],
            [
                "git fetch upstream master",
                "git fetch upstream +pull/707/head:refs/robert/reviews/pr-707",
                "git worktree add /tmp/repo/.worktrees/review__pr-707-review-feature-pr -B review/pr-707-review-feature-pr refs/robert/reviews/pr-707",
            ],
        )

    def test_review_worktree_refreshes_reused_branch_to_latest_pr_head(self):
        from robert_agent.adapters.git import workspace as worktree

        repo_root = self.root / "repo"
        repo_root.mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        review_path = worktree_root / "review__pr-707-review-feature-pr"
        calls = []

        class Completed:
            stdout = (
                f"worktree {repo_root}\n"
                "HEAD abc\n"
                "branch refs/heads/master\n\n"
                f"worktree {review_path}\n"
                "HEAD old\n"
                "branch refs/heads/review/pr-707-review-feature-pr\n\n"
            )

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            result = Completed()
            if args == ["git", "status", "--porcelain"]:
                result.stdout = ""
            return result

        original_run = worktree.subprocess.run
        try:
            worktree.subprocess.run = fake_run
            result = worktree.plan_review_worktree(
                repo_root=repo_root,
                worktree_root=worktree_root,
                source_number=707,
                short_slug="Review Feature PR",
                base_branch="master",
                dry_run=False,
            )
        finally:
            worktree.subprocess.run = original_run

        self.assertEqual(result["mode"], "reuse_existing_worktree")
        self.assertEqual(
            [call[0] for call in calls],
            [
                ["git", "worktree", "list", "--porcelain"],
                ["git", "status", "--porcelain"],
                ["git", "fetch", "upstream", "master"],
                [
                    "git",
                    "fetch",
                    "upstream",
                    "+pull/707/head:refs/robert/reviews/pr-707",
                ],
                [
                    "git",
                    "reset",
                    "--hard",
                    "refs/robert/reviews/pr-707",
                ],
            ],
        )
        self.assertEqual(calls[1][1]["cwd"], review_path)
        self.assertEqual(calls[-1][1]["cwd"], review_path)

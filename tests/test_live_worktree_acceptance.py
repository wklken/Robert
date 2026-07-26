from contextlib import closing
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class LiveWorktreeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source_repo = self.root / "source-repo"
        self.source_repo.mkdir()
        (self.source_repo / ".git").mkdir()
        self.source_data_dir = self.root / "source-data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""data_dir: {self.source_data_dir}
database: dd.sqlite3
max_concurrency: 1
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: main
    repo_root: {self.source_repo}
    worktree_root: {self.source_repo / ".worktrees"}
""",
            encoding="utf-8",
        )

    def test_live_worktree_acceptance_creates_real_git_worktree_in_isolated_checkout(self):
        from robert_agent import live_worktree_acceptance
        workspace_dir = (self.root / "worktree-acceptance").resolve()

        result = live_worktree_acceptance.live_worktree_acceptance(
            self.config_path,
            workspace_dir=workspace_dir,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route_id"], "new-pr")
        self.assertEqual(result["attempt_status"], "running")
        self.assertEqual(result["branch_name"], "codex/dd-77-fix-worktree-acceptance")
        self.assertTrue(Path(result["worktree_path"]).is_dir())
        self.assertTrue(str(Path(result["worktree_path"])).startswith(str(workspace_dir)))
        self.assertEqual(result["git_branch"], result["branch_name"])
        self.assertTrue(result["git_worktree_list_contains_branch"])
        self.assertFalse((self.source_data_dir / "dd.sqlite3").exists())
        with closing(sqlite3.connect(result["db_path"])) as conn, conn:
            attempt = conn.execute(
                "SELECT status, worktree_path, branch_name FROM attempts"
            ).fetchone()
        self.assertEqual(
            attempt,
            ("running", result["worktree_path"], result["branch_name"]),
        )

    def _git(self, args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def _existing_pr_fixture(self):
        upstream = self.root / "upstream.git"
        seed = self.root / "seed"
        checkout = self.root / "checkout"
        worktrees = self.root / "write-worktrees"
        self._git(["init", "--bare", str(upstream)])
        self._git(["clone", str(upstream), str(seed)])
        self._git(["config", "user.name", "Robert Test"], cwd=seed)
        self._git(["config", "user.email", "robert@example.com"], cwd=seed)
        (seed / "value.txt").write_text("base\n", encoding="utf-8")
        self._git(["add", "value.txt"], cwd=seed)
        self._git(["commit", "-m", "base"], cwd=seed)
        self._git(["branch", "-M", "main"], cwd=seed)
        self._git(["push", "origin", "main"], cwd=seed)
        base_sha = self._git(["rev-parse", "HEAD"], cwd=seed)
        self._git(["checkout", "-b", "codex/fix"], cwd=seed)
        (seed / "value.txt").write_text("topic\n", encoding="utf-8")
        self._git(["commit", "-am", "topic"], cwd=seed)
        self._git(["push", "origin", "codex/fix"], cwd=seed)
        head_sha = self._git(["rev-parse", "HEAD"], cwd=seed)
        self._git(
            [
                "--git-dir",
                str(upstream),
                "update-ref",
                "refs/pull/13/head",
                head_sha,
            ]
        )
        self._git(["clone", str(upstream), str(checkout)])
        self._git(["remote", "add", "upstream", str(upstream)], cwd=checkout)
        return checkout, worktrees, head_sha, base_sha

    def test_existing_pr_write_worktree_uses_exact_head_and_base_refs(self):
        from robert_agent import worktree

        checkout, worktrees, head_sha, base_sha = self._existing_pr_fixture()

        result = worktree.prepare_existing_pr_worktree(
            checkout,
            worktrees,
            source_number=13,
            head_branch="codex/fix",
            head_sha=head_sha,
            base_branch="main",
            base_sha=base_sha,
            dry_run=False,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["observed_head_sha"], head_sha)
        self.assertEqual(result["observed_base_sha"], base_sha)
        self.assertEqual(
            self._git(["rev-parse", "HEAD"], cwd=result["worktree_path"]),
            head_sha,
        )
        self.assertEqual(
            self._git(
                ["rev-parse", "refs/robert/prs/pr-13-base"],
                cwd=checkout,
            ),
            base_sha,
        )
        self.assertFalse(
            any(
                forbidden in command
                for command in result["commands"]
                for forbidden in ["--force", " rebase ", "clean -"]
            )
        )

    def test_existing_pr_write_worktree_blocks_dirty_reuse_and_stale_sha(self):
        from robert_agent import worktree

        checkout, worktrees, head_sha, base_sha = self._existing_pr_fixture()
        first = worktree.prepare_existing_pr_worktree(
            checkout,
            worktrees,
            source_number=13,
            head_branch="codex/fix",
            head_sha=head_sha,
            base_branch="main",
            base_sha=base_sha,
            dry_run=False,
        )
        worktree_path = Path(first["worktree_path"])
        (worktree_path / "dirty.txt").write_text("do not discard\n", encoding="utf-8")

        dirty = worktree.prepare_existing_pr_worktree(
            checkout,
            worktrees,
            source_number=13,
            head_branch="codex/fix",
            head_sha=head_sha,
            base_branch="main",
            base_sha=base_sha,
            dry_run=False,
        )
        stale = worktree.prepare_existing_pr_worktree(
            checkout,
            worktrees,
            source_number=13,
            head_branch="codex/other",
            head_sha="0" * 40,
            base_branch="main",
            base_sha=base_sha,
            dry_run=False,
        )

        self.assertFalse(dirty["ok"])
        self.assertEqual(dirty["status"], "blocked_dirty_worktree")
        self.assertTrue((worktree_path / "dirty.txt").exists())
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["status"], "stale_snapshot")

    def test_conflict_remediation_real_merge_graph_passes_attestation(self):
        from robert_agent import remediation, worktree

        checkout, worktrees, head_sha, _base_sha = self._existing_pr_fixture()
        seed = self.root / "seed"
        self._git(["checkout", "main"], cwd=seed)
        (seed / "value.txt").write_text("updated base\n", encoding="utf-8")
        self._git(["commit", "-am", "update base"], cwd=seed)
        self._git(["push", "origin", "main"], cwd=seed)
        base_sha = self._git(["rev-parse", "HEAD"], cwd=seed)

        prepared = worktree.prepare_existing_pr_worktree(
            checkout,
            worktrees,
            source_number=13,
            head_branch="codex/fix",
            head_sha=head_sha,
            base_branch="main",
            base_sha=base_sha,
            dry_run=False,
        )
        worktree_path = Path(prepared["worktree_path"])
        merge = subprocess.run(
            [
                "git",
                "merge",
                "--no-edit",
                "refs/robert/prs/pr-13-base",
            ],
            cwd=worktree_path,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(merge.returncode, 0)
        (worktree_path / "value.txt").write_text(
            "topic on updated base\n",
            encoding="utf-8",
        )
        self._git(["add", "value.txt"], cwd=worktree_path)
        self._git(
            ["commit", "-m", "Resolve base conflict"],
            cwd=worktree_path,
        )

        attestation = remediation.attest_remediation_worktree(
            task_kind="merge_conflict_remediation",
            action_scope={
                "worktree_path": str(worktree_path),
                "branch_name": prepared["branch_name"],
            },
            remediation_evidence={
                "kind": "merge_conflict",
                "episode_id": "episode-conflict",
                "observed_head_sha": head_sha,
                "observed_base_sha": base_sha,
                "resolution_summary": "Resolved value.txt.",
            },
        )

        self.assertEqual(attestation["status"], "accepted")
        self.assertEqual(
            self._git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=worktree_path,
            ),
            "",
        )

    def test_existing_pr_write_worktree_never_resets_unmanaged_checkout(self):
        from robert_agent import worktree

        _checkout, worktrees, head_sha, base_sha = (
            self._existing_pr_fixture()
        )
        seed = self.root / "seed"
        before = self._git(["rev-parse", "HEAD"], cwd=seed)

        blocked = worktree.prepare_existing_pr_worktree(
            seed,
            worktrees,
            source_number=13,
            head_branch="codex/fix",
            head_sha=head_sha,
            base_branch="main",
            base_sha=base_sha,
            dry_run=False,
        )

        self.assertFalse(blocked["ok"])
        self.assertEqual(
            blocked["status"],
            "blocked_unmanaged_worktree",
        )
        self.assertEqual(
            self._git(["rev-parse", "HEAD"], cwd=seed),
            before,
        )


if __name__ == "__main__":
    unittest.main()

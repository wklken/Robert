from pathlib import Path
import subprocess
import sys
import json
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.git.workspace import (
    plan_analysis_worktree,
    plan_review_worktree,
)


class WorktreeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.upstream = root / "upstream"
        self.repo = root / "repo"
        self.managed = root / "managed"
        self.upstream.mkdir()
        self.git(self.upstream, "init", "-b", "main")
        self.git(self.upstream, "config", "user.email", "test@example.invalid")
        self.git(self.upstream, "config", "user.name", "Test")
        (self.upstream / "data").write_text("v1")
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "initial")
        self.git(self.upstream, "update-ref", "refs/pull/42/head", "HEAD")
        self.git(root, "clone", str(self.upstream), str(self.repo))
        self.git(self.repo, "remote", "rename", "origin", "upstream")

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()

    def test_review_rejects_unmanaged_worktree(self):
        human = Path(self.tmp.name) / "human"
        self.git(self.repo, "worktree", "add", "-b", "review/pr-42-title", str(human), "HEAD")
        (human / "data").write_text("human changes")
        result = plan_review_worktree(self.repo, self.managed, 42, "title", "main", dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_unmanaged_worktree")
        self.assertEqual((human / "data").read_text(), "human changes")

    def test_worktree_creation_keeps_control_stdout_valid_json(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "robert_agent.adapters.git.workspace",
                "--repo-root",
                str(self.repo),
                "--worktree-root",
                str(self.managed),
                "--source-number",
                "205",
                "--short-slug",
                "warning",
                "--base-branch",
                "main",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(Path(payload["worktree_path"]).is_dir())

    def test_review_rejects_dirty_managed_worktree(self):
        first = plan_review_worktree(self.repo, self.managed, 42, "title", "main", dry_run=False)
        tree = Path(first["worktree_path"])
        (tree / "data").write_text("human changes")
        result = plan_review_worktree(self.repo, self.managed, 42, "title", "main", dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_dirty_worktree")
        self.assertEqual((tree / "data").read_text(), "human changes")

    def test_analysis_refreshes_clean_reused_worktree(self):
        first = plan_analysis_worktree(self.repo, self.managed, 42, "main", dry_run=False)
        (self.upstream / "data").write_text("v2")
        self.git(self.upstream, "commit", "-am", "advance")
        result = plan_analysis_worktree(self.repo, self.managed, 42, "main", dry_run=False)
        self.assertTrue(result["ok"])
        self.assertEqual(Path(first["worktree_path"], "data").read_text(), "v2")

    def test_analysis_preserves_dirty_reused_worktree(self):
        first = plan_analysis_worktree(self.repo, self.managed, 42, "main", dry_run=False)
        tree = Path(first["worktree_path"])
        (tree / "data").write_text("local changes")
        result = plan_analysis_worktree(self.repo, self.managed, 42, "main", dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual((tree / "data").read_text(), "local changes")

    def test_ordinary_plan_rejects_unmanaged_and_dirty_reuse(self):
        from robert_agent.adapters.git.workspace import plan_worktree

        human = Path(self.tmp.name) / "human"
        self.git(self.repo, "worktree", "add", "-b", "codex/dd-7-task", str(human), "HEAD")
        self.assertEqual(
            plan_worktree(self.repo, self.managed, 7, "task", "main", dry_run=False)["status"],
            "blocked_unmanaged_worktree",
        )
        first = plan_worktree(self.repo, self.managed, 8, "task", "main", dry_run=False)
        tree = Path(first["worktree_path"])
        (tree / "data").write_text("uncommitted")
        self.assertEqual(
            plan_worktree(self.repo, self.managed, 8, "task", "main", dry_run=False)["status"], "blocked_dirty_worktree"
        )
        self.assertEqual((tree / "data").read_text(), "uncommitted")

    def test_existing_pr_fetches_head_missing_from_local_checkout(self):
        from robert_agent.adapters.git.workspace import plan_worktree

        self.git(self.upstream, "checkout", "-b", "codex/pr-fix")
        (self.upstream / "data").write_text("PR fix")
        self.git(self.upstream, "commit", "-am", "PR fix")
        self.git(self.upstream, "update-ref", "refs/pull/42/head", "HEAD")
        expected_head = self.git(self.upstream, "rev-parse", "HEAD")

        result = plan_worktree(
            self.repo, self.managed, 42, "fix", "main", existing_pr_head_branch="codex/pr-fix", dry_run=False
        )

        self.assertTrue(result["ok"])
        tree = Path(result["worktree_path"])
        self.assertEqual(self.git(tree, "rev-parse", "HEAD"), expected_head)
        self.assertEqual(self.git(tree, "branch", "--show-current"), "codex/pr-fix")
        self.assertEqual((tree / "data").read_text(), "PR fix")

    def test_existing_pr_fetch_advances_unchecked_local_branch(self):
        from robert_agent.adapters.git.workspace import plan_worktree

        self.git(self.repo, "branch", "codex/pr-fix", "HEAD")
        (self.upstream / "data").write_text("new PR head")
        self.git(self.upstream, "commit", "-am", "advance PR")
        self.git(self.upstream, "update-ref", "refs/pull/42/head", "HEAD")
        expected_head = self.git(self.upstream, "rev-parse", "HEAD")

        result = plan_worktree(
            self.repo, self.managed, 42, "fix", "main", existing_pr_head_branch="codex/pr-fix", dry_run=False
        )

        self.assertEqual(self.git(result["worktree_path"], "rev-parse", "HEAD"), expected_head)

    def test_existing_pr_fetch_failure_preserves_local_branch_and_creates_no_worktree(self):
        from robert_agent.adapters.git.workspace import plan_worktree

        self.git(self.repo, "branch", "codex/pr-fix", "HEAD")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.git(self.repo, "config", "user.name", "Test")
        local_head = self.git(self.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "local work")
        self.git(self.repo, "update-ref", "refs/heads/codex/pr-fix", local_head)
        (self.upstream / "data").write_text("remote work")
        self.git(self.upstream, "commit", "-am", "remote work")
        self.git(self.upstream, "update-ref", "refs/pull/42/head", "HEAD")

        for number in (42, 404):
            with self.subTest(number=number):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    plan_worktree(
                        self.repo,
                        self.managed,
                        number,
                        "fix",
                        "main",
                        existing_pr_head_branch="codex/pr-fix",
                        dry_run=False,
                    )
                self.assertEqual(raised.exception.cmd[:3], ["git", "fetch", "upstream"])
                self.assertEqual(self.git(self.repo, "rev-parse", "codex/pr-fix"), local_head)
                self.assertFalse((self.managed / "codex__pr-fix").exists())

    def test_existing_dispatch_workspace_is_checked_before_reuse(self):
        from robert_agent.execution.application.dispatch import ensure_dispatch_worktree
        from robert_agent.adapters.git.workspace import plan_worktree

        first = plan_worktree(self.repo, self.managed, 9, "task", "main", dry_run=False)
        tree = Path(first["worktree_path"])
        (tree / "data").write_text("uncommitted")
        result = ensure_dispatch_worktree(
            None, {"repo_root": str(self.repo), "worktree_root": str(self.managed)}, {}, first
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked_dirty_worktree")

    def test_dispatch_rejects_unregistered_directory_and_changed_branch(self):
        from robert_agent.execution.application.dispatch import ensure_dispatch_worktree
        from robert_agent.adapters.git.workspace import plan_worktree

        repo = {"repo_root": str(self.repo), "worktree_root": str(self.managed)}
        ordinary = self.managed / "ordinary"
        ordinary.mkdir(parents=True)
        outcome = ensure_dispatch_worktree(None, repo, {}, {"worktree_path": str(ordinary)})
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["status"], "blocked_unregistered_worktree")
        prepared = plan_worktree(self.repo, self.managed, 10, "task", "main", dry_run=False)
        self.git(prepared["worktree_path"], "checkout", "-b", "different-branch")
        outcome = ensure_dispatch_worktree(None, repo, {}, prepared)
        self.assertEqual(outcome["status"], "blocked_changed_branch")

    def test_owned_resume_keeps_edits_but_rejects_wrong_owner(self):
        from contextlib import closing
        import sqlite3
        from tests.support.board import BoardReadModelFixture
        from robert_agent.adapters.git.workspace import plan_worktree
        from robert_agent.execution.application.dispatch import ensure_dispatch_worktree

        fixture = BoardReadModelFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item = fixture._create("resume", start=True)
        first = plan_worktree(self.repo, self.managed, 11, "resume", "main", dry_run=False)
        tree = Path(first["worktree_path"])
        (tree / "data").write_text("previous attempt edits")
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            task_id = conn.execute(
                "SELECT active_task_id FROM workstreams WHERE workstream_id=?", (item["workstream_id"],)
            ).fetchone()[0]
            for attempt, number, metadata in [
                ("old", 1, {}),
                ("new", 2, {"resume": {"strategy": "resume_from_worktree", "previous_attempt_id": "old"}}),
            ]:
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, metadata_json) VALUES (?, ?, ?, 'prepared', ?, ?, ?)",
                    (attempt, task_id, number, str(tree), first["branch_name"], json.dumps(metadata)),
                )
        repo = {"repo_root": str(self.repo), "worktree_root": str(self.managed)}
        info = {"task_id": task_id, "attempt_id": "new"}
        result = ensure_dispatch_worktree(fixture.db_path, repo, info, first)
        self.assertTrue(result["ok"], result)
        self.assertEqual((tree / "data").read_text(), "previous attempt edits")
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET branch_name='another' WHERE attempt_id='old'")
        self.assertEqual(
            ensure_dispatch_worktree(fixture.db_path, repo, info, first)["status"], "blocked_dirty_worktree"
        )

    def test_registered_path_cannot_be_replaced_by_another_repository(self):
        import shutil
        from robert_agent.adapters.git.workspace import plan_worktree
        from robert_agent.execution.application.dispatch import ensure_dispatch_worktree

        prepared = plan_worktree(self.repo, self.managed, 12, "identity", "main", dry_run=False)
        tree = Path(prepared["worktree_path"])
        shutil.rmtree(tree)
        tree.mkdir()
        self.git(tree, "init", "-b", prepared["branch_name"])
        self.git(tree, "config", "user.email", "test@example.invalid")
        self.git(tree, "config", "user.name", "Test")
        self.git(tree, "commit", "--allow-empty", "-m", "unrelated")
        result = ensure_dispatch_worktree(
            None, {"repo_root": str(self.repo), "worktree_root": str(self.managed)}, {}, prepared
        )
        self.assertEqual(result["status"], "blocked_changed_repository")

    def test_same_named_tag_does_not_change_checkout_branch_identity(self):
        from robert_agent.adapters.git.workspace import plan_worktree
        from robert_agent.execution.application.dispatch import ensure_dispatch_worktree

        prepared = plan_worktree(self.repo, self.managed, 13, "tagged", "main", dry_run=False)
        self.git(self.repo, "tag", prepared["branch_name"])
        result = ensure_dispatch_worktree(
            None, {"repo_root": str(self.repo), "worktree_root": str(self.managed)}, {}, prepared
        )
        self.assertTrue(result["ok"], result)

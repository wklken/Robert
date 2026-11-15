"""Exercise native code publication proof using Git objects in a temporary worktree."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from robert_agent.adapters.git import native_publication
from robert_agent.delivery import tapd_policy


class NativeGitTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "user.name", "Robert CI")
        self.git(self.repo, "config", "user.email", "robert@example.com")
        self.git(self.repo, "remote", "add", "upstream", "https://github.com/org/repo.git")
        self.git(self.repo, "remote", "add", "origin", "git@github.com:robot/repo.git")
        initial = self.commit(self.repo, "main.py", "initial", "Prior human commit")
        self.git(self.repo, "update-ref", "refs/remotes/upstream/main", initial)
        self.key = "a" * 32
        self.branch = f"codex/dd-{self.key}-source-work"
        self.worktree_root = self.root / "trees"
        self.worktree_root.mkdir()
        self.tree = self.worktree_root / "new"
        self.git(self.repo, "worktree", "add", "-b", self.branch, str(self.tree))
        self.purpose = {
            "mode": "new_branch",
            "repo_root": str(self.repo),
            "worktree_root": str(self.worktree_root),
            "workspace_key": self.key,
            "branch_name": self.branch,
            "commit_message": tapd_policy.NATIVE_COMMIT_MESSAGE,
        }

    def git(self, directory, *args):
        return subprocess.run(["git", *args], cwd=directory, text=True, capture_output=True, check=True).stdout.strip()

    def commit(self, directory, path, content, message):
        (directory / path).write_text(content)
        self.git(directory, "add", "--", path)
        self.git(directory, "commit", "-m", message)
        return self.git(directory, "rev-parse", "HEAD")

    def test_readonly_recovery_rejects_index_flags_that_hide_modified_files(self):
        self.git(self.repo, "update-index", "--assume-unchanged", "main.py")
        (self.repo / "main.py").write_text("Changed evidence hidden from git status")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        with self.assertRaises(ValueError):
            native_publication.readonly_snapshot(self.repo, "org/repo")

    def test_readonly_recovery_rechecks_original_head_and_upstream(self):
        from robert_agent.delivery.application import native_preparation

        snapshot = native_publication.readonly_snapshot(self.repo, "org/repo")
        self.assertTrue(native_preparation.recheck_readonly(snapshot))
        self.commit(self.repo, "main.py", "new accepted code", "New human commit")
        self.assertFalse(native_preparation.recheck_readonly(snapshot))
        current = native_publication.readonly_snapshot(self.repo, "org/repo")
        self.git(self.repo, "remote", "set-url", "upstream", "https://github.com/other/repository.git")
        self.assertFalse(native_preparation.recheck_readonly(current))

    def test_approved_metadata_is_bound_to_git_changes_and_exact_head(self):
        pin = native_publication.pin(self.tree, self.purpose, "org/repo", "main")
        first = self.commit(self.tree, "main.py", "updated", tapd_policy.NATIVE_COMMIT_MESSAGE)
        head = self.commit(self.tree, "other.py", "added", tapd_policy.NATIVE_COMMIT_MESSAGE)
        proof = native_publication.inspect(self.tree, self.purpose, pin, expected_head=head)
        self.assertEqual(proof.commits, (head, first))
        self.assertEqual(proof.changes, (("modified", "main.py"), ("added", "other.py")))
        self.commit(self.tree, "main.py", "later", tapd_policy.NATIVE_COMMIT_MESSAGE)
        with self.assertRaises(ValueError):
            native_publication.inspect(self.tree, self.purpose, pin, expected_head=head)

    def test_trailer_foreign_remote_and_unmapped_file_are_denied(self):
        pin = native_publication.pin(self.tree, self.purpose, "org/repo", "main")
        head = self.commit(self.tree, "main.py", "updated", tapd_policy.NATIVE_COMMIT_MESSAGE + "\n\nTicket: pending")
        with self.assertRaises(ValueError):
            native_publication.inspect(self.tree, self.purpose, pin, expected_head=head)
        self.git(self.tree, "commit", "--amend", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        head = self.git(self.tree, "rev-parse", "HEAD")
        self.git(self.repo, "remote", "set-url", "--push", "origin", "https://github.com/other/unrelated.git")
        with self.assertRaises(ValueError):
            native_publication.inspect(self.tree, self.purpose, pin, expected_head=head)
        self.assertEqual(native_publication.mapped_files(self.repo, {"main.py", "invented.py"}), {"main.py"})

    def test_pinned_existing_pr_base_exempts_historical_upstream_commits(self):
        initial = self.git(self.tree, "rev-parse", "HEAD")
        self.git(self.repo, "config", "user.name", "Prior Human")
        base = self.commit(self.repo, "upstream.py", "upstream code", "Upstream history is not Robert output")
        self.git(self.repo, "config", "user.name", "Robert CI")
        first_head = self.commit(self.tree, "pr.py", "PR history", "Prior human PR commit")
        purpose = {
            **self.purpose,
            "mode": "existing_pr",
            "pr": {
                "head_sha": first_head,
                "base_sha": base,
                "base_branch": "main",
                "branch_name": self.branch,
                "head_repository": "robot/repo",
            },
        }
        self.assertNotEqual(initial, base)
        pin = native_publication.pin(self.tree, purpose, "org/repo", "main")
        self.git(self.tree, "merge", "--no-ff", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE, base)
        head = self.git(self.tree, "rev-parse", "HEAD")
        proof = native_publication.inspect(self.tree, purpose, pin, expected_head=head)
        self.assertEqual(proof.commits, (head,))
        self.assertEqual(proof.changes, (("added", "upstream.py"),))
        self.git(self.tree, "switch", "-c", "foreign")
        with self.assertRaises(ValueError):
            native_publication.inspect(self.tree, purpose, pin, expected_head=head)

    def test_tapd_reply_requires_mapped_paths_and_controller_pr_overlay(self):
        from robert_agent.delivery.application import native_preparation

        def result(body):
            return {"planned_actions": [{"provider": "tapd", "body": body}]}

        body = "Verified main.py"
        proof = native_preparation.prepare_tapd(result(body), self.repo, None)
        self.assertEqual(proof["mapped_paths"], ["main.py"])
        self.assertIsNone(native_preparation.prepare_tapd(result("Verified invented.py"), self.repo, None))
        url = "https://github.com/org/repo/pull/12"
        self.assertIsNone(native_preparation.prepare_tapd(result(body + " " + url), self.repo, None))
        self.assertEqual(tapd_policy.append_confirmed_pr_urls(body, {"main.py"}, [url]), body + "\n\n" + url)

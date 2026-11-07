import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.delivery.application import scope as action_scope
from robert_agent.supervision import git as supervision_git


def git(path, *args):
    return subprocess.check_output(
        ["git", *args],
        cwd=path,
        text=True,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).strip()


class RepairGitTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.private = self.root / "private"
        self.private.mkdir()
        git(self.checkout, "init", "-q", "-b", "maintenance")
        git(self.checkout, "config", "user.email", "fixture@example.test")
        git(self.checkout, "config", "user.name", "Fixture")
        self.file = self.checkout / "owned.txt"
        self.file.write_text("original\n")
        git(self.checkout, "add", "owned.txt")
        git(self.checkout, "commit", "-qm", "base")
        self.base = git(self.checkout, "rev-parse", "HEAD")
        self.marker = self.root / "escaped"
        self.script = self.checkout / ".git" / "hooks" / "escape"
        self.script.write_text("#!/bin/sh\nprintf escaped > " + shlex.quote(str(self.marker)) + "\n")
        self.script.chmod(0o700)
        self.scope = {
            "supervision_repair": True,
            "worktree_path": str(self.checkout),
            "branch_name": "maintenance",
            "repo": "owner/maintenance",
            "repair_git": {
                "worktree": str(self.checkout),
                "branch": "maintenance",
                "base_sha": self.base,
                "remote": "https://github.com/owner/maintenance.git",
                "private_root": str(self.private),
            },
        }

    def test_host_git_ignores_model_fsmonitor_and_assume_unchanged(self):
        git(self.checkout, "update-index", "--assume-unchanged", "owned.txt")
        git(self.checkout, "config", "core.fsmonitor", str(self.script))
        self.file.write_text("changed by model\n")
        observed = action_scope.observe_worktree_status(self.scope)
        self.assertFalse(self.marker.exists(), "model fsmonitor executed by host Git")
        self.assertEqual(observed, "dirty")

    def test_model_include_filters_textconv_and_alternates_are_inert(self):
        hostile = self.checkout / ".git" / "hostile-config"
        hostile.write_text(
            f'[filter "escape"]\n    clean = {self.script}\n'
            f'[diff "escape"]\n    textconv = {self.script}\n'
            f"[core]\n    fsmonitor = {self.script}\n"
        )
        (self.checkout / ".gitattributes").write_text("owned.txt filter=escape diff=escape\n")
        git(self.checkout, "add", ".gitattributes")
        git(self.checkout, "commit", "-qm", "attributes")
        git(self.checkout, "config", "include.path", str(hostile))
        (self.checkout / ".git" / "objects" / "info" / "alternates").write_text(str(self.root / "missing") + "\n")
        self.assertEqual(action_scope.observe_worktree_status(self.scope), "clean")
        self.assertFalse(self.marker.exists())
        self.assertTrue(
            supervision_git.clean_revision(self.checkout, action_scope.observe_head(self.scope), self.private)
        )
        self.assertFalse(self.marker.exists())

    def test_model_git_metadata_links_and_pack_corruption_fail_closed(self):
        metadata = self.checkout / ".git"
        head = metadata / "HEAD"
        original = self.root / "head"
        head.rename(original)
        for symlink in (True, False):
            with self.subTest(symlink=symlink):
                if symlink:
                    head.symlink_to(original)
                else:
                    os.link(original, head)
                with self.assertRaises(OSError):
                    action_scope.observe_head(self.scope)
                head.unlink()
        original.rename(head)
        git(self.checkout, "gc")
        self.assertEqual(action_scope.observe_head(self.scope), self.base)
        pack = next((metadata / "objects" / "pack").glob("*.pack"))
        pack.chmod(0o600)
        with pack.open("ab") as output:
            output.write(b"corrupted pack")
        with self.assertRaises(OSError):
            action_scope.observe_head(self.scope)

    def test_staged_only_change_and_missing_binding_are_never_clean(self):
        self.file.write_text("staged\n")
        git(self.checkout, "add", "owned.txt")
        self.file.write_text("original\n")
        self.assertEqual(action_scope.observe_worktree_status(self.scope), "dirty")
        with self.assertRaises(OSError):
            action_scope.observe_head({**self.scope, "repair_git": None})

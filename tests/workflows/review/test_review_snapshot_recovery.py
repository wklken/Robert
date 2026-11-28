from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.workflows.review import workspace
from tests.support.review_workspace import ReviewWorkspaceFixture


class ReviewSnapshotRecoveryTests(ReviewWorkspaceFixture):
    def test_interrupted_isolated_review_keeps_the_read_only_snapshot(self):
        round_id = "interrupted-isolated"
        with patch.object(workspace, "runtime_info", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.prepare(round_id, isolated_checks=True)
        manifest = self.freeze(self.prepare(round_id, isolated_checks=True))
        self.assertEqual(manifest["prompt_version"], workspace.ISOLATED_CHECKS_PROMPT_VERSION)
        self.assertEqual(manifest["source_hash_version"], 2)
        tree = Path(manifest["worktree_path"])
        self.assertEqual(tree.stat().st_mode & 0o222, 0)
        self.assertEqual((tree / "source.py").stat().st_mode & 0o222, 0)
        self.assertEqual(self.prepare(round_id, isolated_checks=True), manifest)

    def test_same_round_recovers_interrupted_controller_preparation(self):
        for index, failure in enumerate((OSError("temporary runtime failure"), KeyboardInterrupt())):
            round_id = "interrupted-" + str(index)
            with self.subTest(failure=type(failure).__name__):
                with patch.object(workspace, "runtime_info", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        self.prepare(round_id)
                manifest = self.freeze(self.prepare(round_id))
                self.assertTrue(workspace.verify_snapshot(manifest))
                self.assertEqual(self.prepare(round_id), manifest)
        trees = self.git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual(trees.count("worktree "), 3)

    def test_recovery_refuses_a_changed_partial_worktree(self):
        with patch.object(workspace, "runtime_info", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.prepare("changed-partial")
        tree = next((self.root / "worktrees").glob("review/pr-42/changed-partial/*"))
        source = tree / "source.py"
        source.write_text("unregistered edits\n")
        with self.assertRaises(ValueError):
            self.prepare("changed-partial")
        self.assertEqual(source.read_text(), "unregistered edits\n")

    def test_recovery_requires_its_own_git_worktree(self):
        worktrees = self.repo / "managed-trees"
        (self.repo / ".git/info/exclude").write_text("managed-trees/\n")
        args = (
            self.repo,
            worktrees,
            self.root / "artifacts",
            "partial",
            "sample/repo",
            "main",
            self.base,
            self.head,
            {},
        )
        with patch.object(workspace, "runtime_info", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                workspace.prepare_candidate_snapshot(*args)
        tree = next(worktrees.glob("review/candidate/partial/*"))
        (tree / ".git").unlink()
        self.assertEqual(self.git(tree, "rev-parse", "--show-toplevel"), str(self.repo))
        with self.assertRaisesRegex(ValueError, "partial review worktree"):
            workspace.prepare_candidate_snapshot(*args)

    def test_published_snapshot_missing_manifest_cannot_be_rebuilt(self):
        manifest = self.freeze(self.prepare("published"))
        context = Path(manifest["context_dir"])
        (context / "manifest.json").unlink()
        with self.assertRaises(ValueError):
            self.prepare("published")
        self.assertTrue(Path(manifest["prompt_path"]).exists())
        self.assertFalse((context / "manifest.json").exists())

    def test_same_snapshot_preparation_does_not_overlap(self):
        entered = threading.Event()
        release = threading.Event()
        original = workspace.runtime_info

        def paused_runtime(*args):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test did not release preparation")
            return original(*args)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(workspace, "runtime_info", side_effect=paused_runtime):
                first = executor.submit(self.prepare, "concurrent")
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    with self.assertRaisesRegex(ValueError, "preparation.*running"):
                        self.prepare("concurrent")
                finally:
                    release.set()
                manifest = first.result(timeout=10)
        self.assertEqual(self.prepare("concurrent"), manifest)

    def add_submodule(self):
        sub = self.root / "component"
        sub.mkdir()
        self.git(sub, "init", "-b", "main")
        self.git(sub, "config", "user.email", "test@example.invalid")
        self.git(sub, "config", "user.name", "Test")
        (sub / "module.py").write_text("value = 1\n")
        self.git(sub, "add", ".")
        self.git(sub, "commit", "-m", "component")
        expected = self.git(sub, "rev-parse", "HEAD")
        self.git(self.upstream, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "vendor/component")
        self.git(self.upstream, "add", ".")
        self.commit("value = 3\n")
        return expected

    def test_gitlink_uses_index_commit_and_allows_correct_initialization(self):
        expected = self.add_submodule()
        manifest = self.freeze(self.prepare("gitlink"))
        self.assertEqual(manifest["source_hashes"]["vendor/component"], "gitlink:" + expected)
        tree = Path(manifest["worktree_path"])
        self.assertTrue(workspace.verify_snapshot(manifest))
        self.git(tree, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
        self.assertTrue(workspace.verify_snapshot(manifest))
        (tree / "vendor/component/module.py").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "dirty"):
            workspace.verify_snapshot(manifest)
        component = tree / "vendor/component"
        self.git(component, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-am", "changed")
        with self.assertRaisesRegex(ValueError, "dirty"):
            workspace.verify_snapshot(manifest)

    def test_read_only_source_keeps_submodules_uninitialized(self):
        self.add_submodule()
        manifest = self.freeze(self.prepare("isolated-gitlink", isolated_checks=True))
        tree = Path(manifest["worktree_path"])
        checkout = workspace.create_check_worktree(manifest, "review-run-" + "a" * 32)
        self.git(checkout, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
        self.assertTrue(workspace.verify_snapshot(manifest))

        component = tree / "vendor/component"
        mode = component.stat().st_mode & 0o777
        component.chmod(mode | 0o200)
        try:
            self.git(tree, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
        finally:
            component.chmod(mode)
        with self.assertRaisesRegex(ValueError, "submodule contains unregistered files"):
            workspace.verify_snapshot(manifest)

    def test_uninitialized_gitlink_cannot_contain_unregistered_source(self):
        self.add_submodule()
        manifest = self.freeze(self.prepare("unregistered-component"))
        component = Path(manifest["worktree_path"]) / "vendor/component"
        (component / "unregistered.py").write_text("value = 0\n")
        with self.assertRaises(ValueError):
            workspace.verify_snapshot(manifest)

    def test_legacy_uninitialized_gitlink_keeps_frozen_contract(self):
        self.add_submodule()
        manifest = self.prepare("legacy-gitlink")
        manifest.pop("source_hash_version", None)
        manifest["source_hashes"]["vendor/component"] = "gitlink:" + self.head
        workspace.review_files.atomic_json(Path(manifest["context_dir"], "manifest.json"), manifest)
        frozen = self.freeze(manifest)
        self.assertTrue(workspace.verify_snapshot(frozen))
        self.assertEqual(self.prepare("legacy-gitlink"), frozen)

    def test_unversioned_snapshot_keeps_its_registered_source_hash_contract(self):
        manifest = self.prepare("legacy-source-hashes")
        manifest.pop("source_hash_version", None)
        workspace.review_files.atomic_json(Path(manifest["context_dir"], "manifest.json"), manifest)
        frozen = self.freeze(manifest)
        self.assertNotIn("source_hash_version", frozen)
        self.assertTrue(workspace.verify_snapshot(frozen))
        self.assertEqual(json.loads(Path(frozen["context_dir"], "manifest.json").read_text()), frozen)

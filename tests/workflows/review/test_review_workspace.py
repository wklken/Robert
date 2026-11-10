import json
from pathlib import Path
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.workflows.review import workspace as review_workspace
from tests.support.review_workspace import ReviewWorkspaceFixture


class ReviewWorkspaceTests(ReviewWorkspaceFixture):
    def test_nested_runtime_scan_does_not_follow_a_link_replacing_deleted_source(self):
        external = self.root / "external-runtime"
        external.mkdir()
        (external / ".python-version").write_text("3.11.6\n")
        (self.repo / "component").symlink_to(external, target_is_directory=True)
        runtime = review_workspace._runtime_info(self.repo, ["component/deleted.py"])
        self.assertNotIn("component/.python-version", runtime["python"]["declarations"])

    def test_runtime_preserves_nested_declarations_for_changed_components(self):
        dashboard = self.upstream / "src" / "dashboard"
        dashboard.mkdir(parents=True)
        (dashboard / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11,<3.12"\n')
        (dashboard / ".python-version").write_text("3.11.6\n")
        (dashboard / "app.py").write_text("value = 1\n")
        self.git(self.upstream, "add", ".")
        self.commit("value = 3\n")
        with patch.object(review_workspace.shutil, "which", return_value=None):
            manifest = self.prepare("nested-runtime")
        runtime = json.loads(Path(manifest["context_dir"], "runtime-info.json").read_text())["python"]
        self.assertEqual(runtime["declarations"]["src/dashboard/.python-version"], "3.11.6")
        self.assertEqual(runtime["declarations"]["src/dashboard/pyproject.toml"], ['">=3.11,<3.12"'])
        self.assertEqual(runtime["status"], "UNRESOLVED")

    def test_two_rounds_use_detached_independent_sources_and_frozen_context(self):
        first = self.freeze(self.prepare("round-1"))
        self.commit("value = 3\n")
        second = self.freeze(self.prepare("round-2"))
        self.assertNotEqual(first["worktree_path"], second["worktree_path"])
        self.assertNotEqual(first["head_sha"], second["head_sha"])
        self.assertEqual(Path(first["worktree_path"], "source.py").read_text(), "value = 2\n")
        self.assertEqual(Path(second["worktree_path"], "source.py").read_text(), "value = 3\n")
        self.assertEqual(
            self.git(Path(first["worktree_path"]), "rev-parse", "--abbrev-ref", "HEAD"),
            "HEAD",
        )
        self.assertTrue(review_workspace.verify_snapshot(first))
        self.assertEqual(self.freeze(first)["prompt_sha256"], first["prompt_sha256"])

    def test_file_commit_before_controller_registration_recovers_without_overwrite(
        self,
    ):
        prepared = self.prepare("round-1")
        frozen = self.freeze(prepared)
        recovered = review_workspace.freeze_context(prepared)
        self.assertEqual(recovered, frozen)
        self.assertEqual(self.prepare("round-1"), frozen)
        Path(frozen["context_dir"], "diff.patch").write_text("corrupted")
        with self.assertRaises(ValueError):
            self.prepare("round-1")

    def test_partial_existing_snapshot_without_manifest_is_rejected(self):
        prepared = self.prepare("round-1")
        Path(prepared["context_dir"], "manifest.json").unlink()
        with self.assertRaises(ValueError):
            self.prepare("round-1")

    def test_forcepush_and_target_branch_change_capture_new_snapshot(self):
        first = self.freeze(self.prepare("round-1"))
        self.git(self.upstream, "reset", "--hard", self.base)
        self.commit("value = 9\n")
        self.git(self.upstream, "branch", "release", self.base)
        second = review_workspace.prepare_snapshot(
            self.repo,
            self.root / "worktrees",
            self.root / "artifacts",
            "round-2",
            {
                "number": 42,
                "head": {"sha": self.head, "ref": "feature"},
                "base": {"sha": self.base, "ref": "release"},
                "user": {"login": "author"},
            },
            [],
        )
        self.assertEqual(second["base_ref"], "release")
        self.assertNotEqual(second["snapshot_id"], first["snapshot_id"])
        self.assertEqual(first["merge_base_sha"], self.base)

    def test_fetch_race_and_source_or_context_changes_block_review(self):
        info = {
            "number": 42,
            "head": {"sha": "0" * 40, "ref": "feature"},
            "base": {"sha": self.base, "ref": "main"},
            "user": {"login": "author"},
        }
        with self.assertRaises(ValueError):
            review_workspace.prepare_snapshot(
                self.repo,
                self.root / "worktrees",
                self.root / "artifacts",
                "race",
                info,
                [],
            )
        manifest = self.freeze(self.prepare("round-1"))
        source = Path(manifest["worktree_path"], "source.py")
        source.write_text("tampered\n")
        with self.assertRaises(ValueError):
            review_workspace.verify_snapshot(manifest)
        source.write_text("value = 2\n")
        Path(manifest["context_dir"], "diff.patch").write_text("tampered")
        with self.assertRaises(ValueError):
            review_workspace.verify_snapshot(manifest)

    def test_advanced_target_branch_preserves_captured_pr_base(self):
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "other.py").write_text("unrelated = True\n")
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "advance target branch")
        self.assertNotEqual(self.git(self.upstream, "rev-parse", "main"), self.base)

        manifest = self.freeze(self.prepare("advanced-base"))

        self.assertEqual(manifest["base_sha"], self.base)
        self.assertEqual(manifest["merge_base_sha"], self.base)
        self.assertEqual(
            Path(manifest["context_dir"], "changed-files.txt").read_text(),
            "source.py\n",
        )
        self.assertTrue(review_workspace.verify_snapshot(manifest))

    def test_unresolved_runtime_and_missing_primary_files(self):
        manifest = self.prepare("round-1")
        runtime = json.loads(Path(manifest["context_dir"], "runtime-info.json").read_text())
        self.assertEqual(runtime["python"]["status"], "UNRESOLVED")
        with self.assertRaises(ValueError):
            review_workspace.freeze_context(manifest)
        self.freeze(manifest)
        Path(manifest["worktree_path"], "untracked").write_text("unexpected")
        with self.assertRaises(ValueError):
            review_workspace.verify_snapshot(manifest)

    def test_prompt_written_before_manifest_can_be_recovered(self):
        from unittest.mock import patch

        manifest = self.prepare("interrupted-freeze")
        with patch.object(review_workspace.review_files, "atomic_json", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.freeze(manifest)
        prompt = Path(manifest["artifact_root"]) / "reviewer-prompt.md"
        before = prompt.read_bytes()
        frozen = review_workspace.freeze_context(manifest)
        self.assertTrue(frozen["frozen"])
        self.assertEqual(prompt.read_bytes(), before)

    def test_unregistered_different_prompt_is_not_adopted(self):
        from unittest.mock import patch

        manifest = self.prepare("changed-prompt")
        with patch.object(review_workspace.review_files, "atomic_json", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.freeze(manifest)
        prompt = Path(manifest["artifact_root"]) / "reviewer-prompt.md"
        prompt.write_text("changed")
        with self.assertRaises(ValueError):
            review_workspace.freeze_context(manifest)

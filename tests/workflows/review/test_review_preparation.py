import json
from pathlib import Path

from robert_agent.workflows.review import preparation
from robert_agent.workflows.review import files
from robert_agent.workflows.review import workspace
from tests.support.review_workspace import ReviewWorkspaceFixture


class ReviewPreparationTests(ReviewWorkspaceFixture):
    def test_inventory_handles_rules_runtime_binary_deletions_and_links(self):
        self.git(self.upstream, "checkout", "main")
        (self.upstream / "deleted.py").write_text("old = True\n")
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "deleted baseline")
        self.base = self.git(self.upstream, "rev-parse", "HEAD")
        self.git(self.upstream, "checkout", "feature")
        self.git(self.upstream, "merge", "main", "--no-edit")
        (self.upstream / "deleted.py").unlink()
        (self.upstream / "AGENTS.md").write_text("Root rules\n")
        (self.upstream / "web").mkdir()
        (self.upstream / "web/AGENTS.override.md").write_text("Specific rules\n")
        (self.upstream / "web/package.json").write_text('{"scripts":{"test":"vitest"}}')
        (self.upstream / "web/file with space.ts").write_text("const value = 1;\n")
        (self.upstream / "web/asset.bin").write_bytes(b"\0\xff\0")
        (self.upstream / "web/link.ts").symlink_to("file with space.ts")
        self.git(self.upstream, "add", "-A")
        self.git(self.upstream, "commit", "-m", "mixed inventory")
        self.head = self.git(self.upstream, "rev-parse", "HEAD")
        self.git(self.upstream, "update-ref", "refs/pull/42/head", "HEAD")
        manifest = self.prepare("inventory")
        preparation.write_context(manifest)
        frozen = workspace.freeze_context(manifest)
        index = json.loads(Path(frozen["context_dir"], "source-index.json").read_text())
        paths = {entry["path"]: entry for entry in index["files"]}
        self.assertIn("web/file with space.ts", paths)
        self.assertIn("project_rules", paths["AGENTS.md"]["roles"])
        self.assertIn("project_rules", paths["web/AGENTS.override.md"]["roles"])
        self.assertIn("runtime_or_checks", paths["web/package.json"]["roles"])
        self.assertNotIn("deleted.py", paths)
        self.assertNotIn("web/link.ts", paths)
        changed = {entry["path"]: entry for entry in index["changes"]}
        self.assertEqual(changed["deleted.py"]["availability"], "absent-at-head")
        self.assertEqual(changed["web/link.ts"]["availability"], "symlink-or-unsafe-path")
        self.assertTrue(changed["web/asset.bin"]["binary"])
        self.assertIn("not read", index["reading_notes"])
        self.assertIn("Tests have not been run", Path(frozen["context_dir"], "context-summary.md").read_text())

    def test_large_request_is_bounded_and_redacted_without_losing_full_inputs(self):
        manifest = self.prepare("bounded")
        context = Path(manifest["context_dir"])
        event = [{"body": "Review /root/private/path on 10.0.0.12 " + "x" * 100000}]
        (context / "github-context.json").write_text(json.dumps(event))
        manifest["files"][str(context / "github-context.json")] = files.sha256_file(context / "github-context.json")
        preparation.write_context(manifest)
        summary = (context / "context-summary.md").read_text()
        self.assertLess(len(summary.encode()), 6000)
        self.assertNotIn("/root/private/path", summary)
        self.assertNotIn("10.0.0.12", summary)
        self.assertIn("github-context.json", summary)
        self.assertEqual(json.loads((context / "github-context.json").read_text()), event)

    def test_sensitive_request_preview_is_omitted(self):
        manifest = self.prepare("sensitive")
        context = Path(manifest["context_dir"])
        secret = "ghp_" + "a" * 24
        (context / "github-context.json").write_text(json.dumps([{"body": secret}]))
        preparation.write_context(manifest)
        summary = (context / "context-summary.md").read_text()
        self.assertNotIn(secret, summary)
        self.assertIn("omitted", summary)

    def test_partial_generation_is_replaced_and_frozen_bytes_are_preserved(self):
        manifest = self.prepare("partial")
        context = Path(manifest["context_dir"])
        (context / "context-summary.md").write_text("interrupted output")
        preparation.write_context(manifest)
        first = {name: (context / name).read_bytes() for name in workspace.PRIMARY_FILES}
        preparation.write_context(manifest)
        self.assertEqual(first, {name: (context / name).read_bytes() for name in workspace.PRIMARY_FILES})
        frozen = workspace.freeze_context(manifest)
        self.assertEqual(workspace.freeze_context(frozen), frozen)
        self.assertEqual(first, {name: (context / name).read_bytes() for name in workspace.PRIMARY_FILES})

    def test_symlink_output_is_refused(self):
        manifest = self.prepare("unsafe-output")
        target = self.root / "untouched"
        target.write_text("original")
        Path(manifest["context_dir"], "context-summary.md").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            preparation.write_context(manifest)
        self.assertEqual(target.read_text(), "original")

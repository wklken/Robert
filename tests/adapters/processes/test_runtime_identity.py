from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

from tests.support import PACKAGE_ROOT  # noqa: F401


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        from robert_agent.adapters.processes import runtime_identity

        self.runtime = runtime_identity
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.package = self.repo / "src" / "robert_agent"
        self.package.mkdir(parents=True)
        (self.package / "__init__.py").write_bytes(b"VALUE = 1\n")
        (self.repo / ".gitignore").write_text("dist/\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Robert Test",
            "-c",
            "user.email=robert@example.invalid",
            "commit",
            "-qm",
            "test",
        )
        self.wheel = self.repo / "dist" / "fixture-1.0-py3-none-any.whl"
        self.wheel.parent.mkdir()
        self.write_wheel(b"VALUE = 1\n")
        self.manifest = self.root / "build.json"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write_wheel(self, module):
        with ZipFile(self.wheel, "w") as output:
            output.writestr("robert_agent/__init__.py", module)
            output.writestr("fixture-1.0.dist-info/METADATA", "Name: fixture\nVersion: 1.0\n")

    def test_build_matches_clean_git_and_runtime_matches_actual_installed_files(self):
        manifest = self.runtime.build_manifest(self.repo, self.wheel)
        self.assertEqual(manifest["source_revision"], self.git("rev-parse", "HEAD"))
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(self.manifest, 0o600)
        started = datetime.now(timezone.utc).isoformat()
        initial = self.runtime.runtime_snapshot(
            run_id="daemon-run-1",
            started_at=started,
            manifest_path=self.manifest,
            package_root=self.package,
        )
        self.assertEqual(initial["build_status"], "verified")
        self.assertEqual(initial["build_id"], manifest["build_id"])
        self.assertEqual(initial["process_identity"]["pid"], os.getpid())
        self.assertEqual(initial["run_id"], "daemon-run-1")
        self.assertEqual(initial["started_at"], started)
        (self.package / "__init__.py").write_bytes(b"VALUE = 2\n")
        changed = self.runtime.runtime_snapshot(
            run_id="daemon-run-2",
            started_at=started,
            manifest_path=self.manifest,
            package_root=self.package,
        )
        self.assertEqual(changed["build_status"], "unverified")
        self.assertNotIn("source_revision", changed)

    def test_manifest_rejects_mismatched_wheel_and_dirty_source(self):
        self.write_wheel(b"VALUE = 9\n")
        with self.assertRaisesRegex(ValueError, "wheel.*Git"):
            self.runtime.build_manifest(self.repo, self.wheel)
        self.write_wheel(b"VALUE = 1\n")
        (self.package / "__init__.py").write_bytes(b"VALUE = 2\n")
        with self.assertRaisesRegex(ValueError, "clean"):
            self.runtime.build_manifest(self.repo, self.wheel)

import importlib
import unittest
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.support import PACKAGE_ROOT
from robert_agent.resource_files import resource


class PackageLayoutTests(unittest.TestCase):
    def test_source_package_directory_exists(self):
        self.assertTrue(PACKAGE_ROOT.is_dir())

    def test_core_modules_import_from_package(self):
        for module_name in [
            "robert_agent.runtime.cycle",
            "robert_agent.runtime.daemon",
            "robert_agent.interfaces.web.server",
            "robert_agent.worker.result",
        ]:
            with self.subTest(module_name=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_documented_recovery_modules_remain_executable(self):
        for module in ("runtime.scan", "runtime.control"):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", "robert_agent." + module, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_background_scan_script_ignores_checkout_package(self):
        from robert_agent.runtime import daemon

        with tempfile.TemporaryDirectory() as temporary:
            shadow = Path(temporary) / "robert_agent"
            shadow.mkdir()
            (shadow / "__init__.py").write_text('raise RuntimeError("untrusted checkout imported")')
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    daemon._script_path("runtime/scan.py"),
                    "--help",
                ],
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_packaged_workflow_exists(self):
        self.assertTrue(resource("workflow.yml").is_file())


if __name__ == "__main__":
    unittest.main()

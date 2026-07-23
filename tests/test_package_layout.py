import importlib
import unittest

from tests.support import PACKAGE_ROOT
from robert_agent.resource_files import resource


class PackageLayoutTests(unittest.TestCase):
    def test_source_package_directory_exists(self):
        self.assertTrue(PACKAGE_ROOT.is_dir())

    def test_core_modules_import_from_package(self):
        for module_name in [
            "robert_agent.run_once",
            "robert_agent.daemon",
            "robert_agent.web",
            "robert_agent.worker.result",
        ]:
            with self.subTest(module_name=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_packaged_workflow_exists(self):
        self.assertTrue(resource("workflow.yml").is_file())


if __name__ == "__main__":
    unittest.main()

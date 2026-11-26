import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.workflows.review.project_runtime import runtime_info


class ProjectRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "checkout"
        self.tree.mkdir()
        self.tools = self.root / "bin"
        self.tools.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"PATH": str(self.tools)}).start()

    def write(self, name, contents):
        path = self.tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def tool(self, name, response):
        path = self.tools / name
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{response}'\n")
        path.chmod(0o755)
        return str(path)

    def test_resolves_changed_components_from_their_own_declarations(self):
        self.write("frontend/.nvmrc", "24.22.1\n")
        self.write("frontend/package.json", json.dumps({"packageManager": "pnpm@11.25.0"}))
        self.write("frontend/pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
        self.write("backend/.python-version", "3.11.6\n")
        self.write("core/go.mod", "module example.org/core\n\ngo 1.24.0\ntoolchain go1.24.2\n")
        node = self.tool("node", "v24.22.1")
        pnpm = self.tool("pnpm", "11.25.0")
        python = self.tool("python3.11", "Python 3.11.6")
        go = self.tool("go", "go version go1.24.4 linux/amd64")

        components = runtime_info(self.tree, ["frontend/src/app.ts", "backend/pkg/test.py", "core/internal/main.go"])[
            "components"
        ]

        self.assertEqual(components["frontend"]["node"]["executable"], node)
        self.assertEqual(components["frontend"]["pnpm"]["executable"], pnpm)
        self.assertEqual(components["frontend"]["node_dependencies"]["status"], "MISSING")
        self.assertEqual(components["backend"]["python"]["executable"], python)
        self.assertEqual(components["core"]["go"]["executable"], go)
        self.assertEqual(components["core"]["go"]["status"], "RESOLVED")

    def test_version_mismatch_does_not_offer_a_command(self):
        self.write("frontend/.nvmrc", "24.22.1\n")
        self.write("frontend/package.json", json.dumps({"packageManager": "pnpm@11.25.0"}))
        self.tool("node", "v24.22.10")
        self.tool("pnpm", "12.23.0")
        tools = runtime_info(self.tree, ["frontend/app.ts"])["components"]["frontend"]
        for kind in ("node", "pnpm"):
            self.assertEqual(tools[kind]["status"], "UNRESOLVED")
            self.assertIsNone(tools[kind]["executable"])
            self.assertIn("mismatch", tools[kind]["reason"])

    def test_probe_ignores_checkout_executables_and_runs_outside_source(self):
        self.write(".nvmrc", "24.22.1\n")
        self.write("bin/node", "#!/bin/sh\nprintf 'v24.22.1\\n'\n")
        source_tool = self.tree / "bin/node"
        source_tool.chmod(0o755)
        (self.tools / "node").symlink_to(source_tool)
        with patch.dict(os.environ, {"PATH": os.pathsep.join((str(self.tools), str(source_tool.parent), "bin"))}):
            unresolved = runtime_info(self.tree, ["app.js"])["components"]["."]["node"]
        self.assertEqual(unresolved["status"], "UNRESOLVED")
        self.assertIsNone(unresolved["executable"])

        (self.tools / "node").unlink()
        self.write(".guard", "present")
        tool = self.tools / "node"
        tool.write_text("#!/bin/sh\ntest ! -e .guard || exit 1\nprintf 'v24.22.1\\n'\n")
        tool.chmod(0o755)
        selected = runtime_info(self.tree, ["app.js"])["components"]["."]["node"]
        self.assertEqual(selected["executable"], str(tool))

    def test_component_constraints_and_workspace_toolchain(self):
        self.write("go.work", "go 1.25.1 // CI version floor\n")
        self.write("service/go.mod", "module example.org/service\ngo 1.24.0\n")
        self.write(".nvmrc", "24.22.1")
        self.write("package.json", json.dumps({"packageManager": "pnpm@11.25.0"}))
        self.write("web/package.json", json.dumps({"engines": {"node": ">=24.22.1 <25"}}))
        self.write("pnpm-lock.yaml", " " * 300_000)
        self.write("web/node_modules/.modules.yaml", "")
        self.tool("go", "go version go1.24.4 linux/amd64")
        self.tool("node", "v24.22.1")
        self.tool("pnpm", "11.25.0")

        result = runtime_info(self.tree, ["service/handler.go", "web/index.ts"])["components"]
        self.assertEqual(result["service"]["go"]["status"], "UNRESOLVED")
        self.assertEqual(result["service"]["go"]["environment"]["GOWORK"], str(self.tree / "go.work"))
        self.assertEqual(result["web"]["node"]["status"], "RESOLVED")
        self.assertEqual(result["web"]["pnpm"]["status"], "RESOLVED")
        self.assertEqual(result["web"]["node_dependencies"]["status"], "PRESENT_UNVERIFIED")
        self.assertEqual(result["web"]["node_dependencies"]["lockfile"], "pnpm-lock.yaml")

    def test_python_pin_and_project_range_must_both_match(self):
        self.write("service/.python-version", "3.11.6")
        self.write("service/pyproject.toml", '[project]\nrequires-python = ">=3.12"\n')
        self.tool("python3.11", "Python 3.11.6")
        result = runtime_info(self.tree, ["service/app.py"])["components"]["service"]["python"]
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["observed"], ["3.11.6"])

    def test_mypy_target_does_not_change_resolved_project_interpreter(self):
        self.write(".python-version", "3.14\n")
        project = '[project]\nrequires-python = ">=3.11"\n'
        self.write("pyproject.toml", project)
        python = self.tool("python3.14", "Python 3.14.7")
        before = runtime_info(self.tree)
        self.assertEqual(before["python"]["status"], "RESOLVED")

        self.write("pyproject.toml", project + '\n[tool.mypy]\npython_version = "3.11"\n')
        after = runtime_info(self.tree)

        self.assertEqual(after["python"]["status"], "RESOLVED")
        self.assertEqual(after["python"]["interpreter"], python)
        self.assertEqual(after["components"]["."]["python"], before["components"]["."]["python"])

    def test_blank_python_pin_leaves_project_range_usable(self):
        self.write(".python-version", " \n")
        self.write("pyproject.toml", '[project]\nrequires-python = ">=3.14"\n')
        python = self.tool("python3", "Python 3.14.7")
        result = runtime_info(self.tree)["components"]["."]["python"]
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(result["executable"], python)

    def test_python_configuration_reads_only_file_specific_runtime_sections(self):
        configurations = {
            "pyproject.toml": (
                '[project]\nrequires-python = """\n>=3.14,<3.15\n"""\n'
                '[tool.example]\nrequires-python = "<3.14"\npython_requires = "<3.14"\n'
                'python_version = "3.11"\n'
            ),
            "setup.cfg": (
                "[DEFAULT]\npython_requires = <3.14\n"
                "[options]\npython_requires =\n    >=3.14,<3.15 # runtime floor\n"
                "[mypy]\npython_version = 3.11\npython_requires = <3.14\n"
            ),
            "Pipfile": (
                '[requires]\npython_version = "3.14"\n[packages]\npython_version = "3.11"\npython_requires = "<3.14"\n'
            ),
        }
        python = self.tool("python3", "Python 3.14.7")
        for name, contents in configurations.items():
            with self.subTest(name=name):
                self.write("service/" + name, contents)
                result = runtime_info(self.tree, ["service/app.py"])["components"]["service"]["python"]
                self.assertEqual(result["status"], "RESOLVED")
                self.assertEqual(result["executable"], python)
                (self.tree / "service" / name).unlink()

    def test_tool_configuration_alone_does_not_declare_a_python_runtime(self):
        configurations = {
            "pyproject.toml": '[tool.mypy]\npython_version = "3.14"\n',
            "setup.cfg": "[mypy]\npython_version = 3.14\n",
            "Pipfile": '[packages]\npython_version = "3.14"\n',
        }
        self.tool("python3", "Python 3.14.7")
        for name, contents in configurations.items():
            with self.subTest(name=name):
                self.write(name, contents)
                result = runtime_info(self.tree)
                self.assertEqual(result["python"]["status"], "UNRESOLVED")
                self.assertIsNone(result["python"]["interpreter"])
                self.assertNotIn("python", result["components"].get(".", {}))
                (self.tree / name).unlink()

        self.write("setup.cfg", "[DEFAULT]\npython_requires = >=3.14\n[options]\npackages = find:\n")
        result = runtime_info(self.tree)
        self.assertEqual(result["python"]["status"], "UNRESOLVED")
        self.assertIsNone(result["python"]["interpreter"])

    def test_each_python_runtime_declaration_can_refuse_an_incompatible_interpreter(self):
        configurations = {
            "pyproject.toml": '[project]\nrequires-python = ">=3.15"\n',
            "setup.cfg": "[options]\npython_requires = >=3.15\n",
            "Pipfile": '[requires]\npython_version = "3.15"\n',
        }
        self.tool("python3", "Python 3.14.7")
        for name, contents in configurations.items():
            with self.subTest(name=name):
                self.write(name, contents)
                result = runtime_info(self.tree)["components"]["."]["python"]
                self.assertEqual(result["status"], "UNRESOLVED")
                self.assertIsNone(result["executable"])
                self.assertEqual(result["observed"], ["3.14.7"])
                (self.tree / name).unlink()

    def test_pipfile_full_version_is_a_runtime_constraint(self):
        self.tool("python3", "Python 3.14.7")
        for version, status in (("3.14.7", "RESOLVED"), ("3.14.6", "UNRESOLVED")):
            with self.subTest(version=version):
                self.write("Pipfile", f'[requires]\npython_version = "3.14"\npython_full_version = "{version}"\n')
                result = runtime_info(self.tree)["components"]["."]["python"]
                self.assertEqual(result["status"], status)
                if status == "UNRESOLVED":
                    self.assertIsNone(result["executable"])

    def test_invalid_python_configuration_does_not_offer_an_interpreter(self):
        self.write(".python-version", "3.14\n")
        self.tool("python3.14", "Python 3.14.7")
        configurations = (
            ("pyproject.toml", '[project]\nrequires-python = ">=3.14\n'),
            ("pyproject.toml", '[project]\nrequires-python = [">=3.14"]\n'),
            ("setup.cfg", "[options\npython_requires = >=3.14\n"),
            ("Pipfile", '[requires]\npython_version = "3.14\n'),
            ("Pipfile", "[requires]\npython_version = 3.14\n"),
        )
        for name, contents in configurations:
            with self.subTest(name=name, contents=contents):
                self.write(name, contents)
                result = runtime_info(self.tree)["components"]["."]["python"]
                self.assertEqual(result["status"], "UNRESOLVED")
                self.assertIsNone(result["executable"])
                (self.tree / name).unlink()

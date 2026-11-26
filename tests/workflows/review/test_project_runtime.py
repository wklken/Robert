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

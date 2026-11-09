"""Keep migrated owners independent of orchestration and workflow implementations."""

import ast
import re
import unittest


from tests.support import PACKAGE_ROOT as PACKAGE

VALUE_CONTRACTS = {
    "robert_agent.foundation.contracts.work",
    "robert_agent.foundation.contracts.execution",
    "robert_agent.foundation.contracts.delivery",
    "robert_agent.foundation.contracts.supervision",
}


def import_graph(package):
    modules = {}
    for path in package.rglob("*.py"):
        parts = path.relative_to(package).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(("robert_agent", *parts))] = path
    graph = {}
    for name, path in modules.items():
        dependencies = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names if alias.name in modules)
            elif isinstance(node, ast.ImportFrom):
                parent = node.module or ""
                if node.level:
                    package_name = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
                    prefix = package_name.split(".")[: len(package_name.split(".")) - node.level + 1]
                    parent = ".".join([*prefix, *([parent] if parent else [])])
                if parent in modules:
                    dependencies.add(parent)
                dependencies.update(
                    f"{parent}.{alias.name}" for alias in node.names if f"{parent}.{alias.name}" in modules
                )
        graph[name] = dependencies
    return graph


class ArchitectureBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = import_graph(PACKAGE)

    def assert_dependencies(self, module, allowed):
        self.assertIn(module, self.graph)
        self.assertEqual(self.graph[module] - set(allowed), set(), module)

    def test_source_import_graph_is_acyclic(self):
        # Include imports inside functions: delaying an import cannot hide a
        # reverse dependency. Dynamic entrypoints have separate smoke tests.
        remaining = set(self.graph)
        while remaining:
            ready = {name for name in remaining if not (self.graph[name] & remaining)}
            self.assertTrue(ready, "cyclic source imports: " + ", ".join(sorted(remaining)))
            remaining.difference_update(ready)

    def test_configuration_imports_only_configuration_and_boundary_capabilities(self):
        allowed = (
            "robert_agent.configuration",
            "robert_agent.resource_files",
            "robert_agent.worker_adapters",
            "robert_agent.foundation.commands",
            "robert_agent.foundation.timestamps",
        )
        for name, dependencies in self.graph.items():
            if name.startswith("robert_agent.configuration."):
                for dependency in dependencies:
                    self.assertTrue(
                        dependency == "robert_agent"
                        or dependency in VALUE_CONTRACTS
                        or any(dependency == prefix or dependency.startswith(prefix + ".") for prefix in allowed),
                        (name, dependency),
                    )

    def test_pure_policies_have_no_application_dependencies(self):
        self.assert_dependencies("robert_agent.work.policies", ())
        self.assert_dependencies("robert_agent.delivery.policies", {"robert_agent.foundation.contracts.delivery"})
        self.assert_dependencies(
            "robert_agent.work.inputs", {"robert_agent.work.errors", "robert_agent.foundation.contracts.work"}
        )

    def test_shared_value_contracts_are_pure_leaves(self):
        for name in VALUE_CONTRACTS:
            self.assert_dependencies(name, ())
            path = PACKAGE.joinpath(*name.split(".")[1:]).with_suffix(".py")
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.module, "enum", (name, node.lineno))
                self.assertNotIsInstance(node, (ast.Import, ast.FunctionDef, ast.AsyncFunctionDef))

    def test_shared_timestamps_are_a_standard_library_leaf(self):
        name = "robert_agent.foundation.timestamps"
        self.assert_dependencies(name, ())
        path = PACKAGE / "foundation/timestamps.py"
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.module, "datetime", (name, node.lineno))
            self.assertNotIsInstance(node, ast.Import)

    def test_leaf_capabilities_do_not_import_their_callers(self):
        lower = (
            "foundation",
            "configuration",
            "work",
            "execution",
            "delivery",
            "supervision",
            "adapters",
            "context",
            "readmodels",
            "security",
            "models",
            "persistence",
        )
        upper = ("application", "runtime", "interfaces", "cli", "operations", "acceptance", "workflows", "worker")
        for name, dependencies in self.graph.items():
            if ".application." in name or name.endswith(".application"):
                continue
            if not any(name.startswith("robert_agent." + owner + ".") for owner in lower):
                continue
            for dependency in dependencies:
                self.assertFalse(
                    ".application." in dependency
                    or dependency.endswith(".application")
                    or any(
                        dependency == "robert_agent." + owner or dependency.startswith("robert_agent." + owner + ".")
                        for owner in upper
                    ),
                    (name, dependency),
                )

    def test_adapters_do_not_import_domain_or_use_case_callers(self):
        allowed = {
            "adapters",
            "foundation",
            "security",
            "configuration",
            "worker_adapters",
            "resource_files",
            "paths",
            "persistence",
        }
        for name, dependencies in self.graph.items():
            if name.startswith("robert_agent.adapters."):
                for dependency in dependencies - {"robert_agent"}:
                    self.assertIn(dependency.split(".")[1], allowed, (name, dependency))

    def test_models_and_persistence_have_no_domain_callers(self):
        for name, dependencies in self.graph.items():
            if name.startswith("robert_agent.models"):
                allowed = {"models"}
            elif name.startswith("robert_agent.persistence"):
                allowed = {"models", "persistence", "resource_files", "security"}
            else:
                continue
            for dependency in dependencies - {"robert_agent"}:
                if name.startswith("robert_agent.persistence.") and (
                    dependency in VALUE_CONTRACTS or dependency == "robert_agent.foundation.timestamps"
                ):
                    continue
                self.assertIn(dependency.split(".")[1], allowed, (name, dependency))

    def test_database_expressions_are_owned_by_persistence(self):
        sql = re.compile(
            r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|PRAGMA\s+\w+)\b",
            re.S,
        )
        for path in PACKAGE.rglob("*.py"):
            owner = path.relative_to(PACKAGE).parts[0]
            if owner == "persistence":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports = (
                        [node.module or ""]
                        if isinstance(node, ast.ImportFrom)
                        else [alias.name for alias in node.names]
                    )
                    if isinstance(node, ast.ImportFrom):
                        imports += [(node.module or "") + "." + alias.name for alias in node.names]
                    for name in imports:
                        if owner != "models":
                            self.assertFalse(
                                name == "robert_agent.models" or name.startswith("robert_agent.models."),
                                (path, node.lineno),
                            )
                            self.assertFalse(
                                any(
                                    name == prefix or name.startswith(prefix + ".")
                                    for prefix in (
                                        "robert_agent.persistence.catalog",
                                        "robert_agent.persistence.statements",
                                        "robert_agent.persistence.expressions",
                                    )
                                ),
                                (path, node.lineno),
                            )
                        self.assertFalse(name.endswith(".execute_control"), (path, node.lineno))
                        self.assertFalse(name == "sqlite3" or name.startswith("sqlite3."), (path, node.lineno))
                        if owner != "models":
                            self.assertFalse(
                                name == "sqlalchemy" or name.startswith("sqlalchemy."), (path, node.lineno)
                            )
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(
                        node.func.attr,
                        {"execute", "executemany", "executescript", "exec_driver_sql", "execute_control"},
                        (path, node.lineno),
                    )
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"_driver", "_connection", "driver_connection"}, (path, node.lineno))
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(sql.search(node.value), (path, node.lineno))

    def test_submission_and_stages_do_not_load_workflow_implementations(self):
        self.assert_dependencies(
            "robert_agent.delivery.contract",
            {
                "robert_agent.delivery",
                "robert_agent.delivery.policies",
                "robert_agent.foundation.contracts.delivery",
                "robert_agent.foundation.contracts.work",
            },
        )
        for dependency in self.graph["robert_agent.execution.stages"]:
            self.assertFalse(
                dependency.startswith(
                    ("robert_agent.workflows.", "robert_agent.application.", "robert_agent.runtime.")
                ),
                dependency,
            )

    def test_migrated_consumers_do_not_borrow_runtime_or_view_functions(self):
        # Missing old modules cannot disappear silently from the import graph.
        root_exports = {"__version__", "paths", "resource_files"} | {
            path.name for path in PACKAGE.iterdir() if path.is_dir() and (path / "__init__.py").exists()
        }
        for path in PACKAGE.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module == "robert_agent":
                    self.assertTrue({alias.name for alias in node.names} <= root_exports, (path, node.lineno))
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("robert_agent."):
                    self.assertIn(node.module, self.graph, (path, node.lineno))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("robert_agent."):
                            self.assertIn(alias.name, self.graph, (path, node.lineno))

    def test_root_contains_only_package_primitives(self):
        self.assertEqual({path.stem for path in PACKAGE.glob("*.py")}, {"__init__", "paths", "resource_files"})

    def test_declared_commands_use_real_implementation_modules(self):
        import importlib
        import json

        commands = json.loads((PACKAGE / "resources/entrypoints.json").read_text())
        self.assertIsInstance(commands, list)
        self.assertEqual(len(commands), len(set(commands)))
        self.assertTrue(
            {
                "robert_agent.runtime.paused_monitor",
                "robert_agent.workflows.maintenance.worker",
                "robert_agent.workflows.review.coordinator",
                "robert_agent.workflows.vote.coordinator",
            }.issubset(commands)
        )
        for name in commands:
            self.assertIn(name, self.graph)
            self.assertGreaterEqual(name.count("."), 2, name)
            self.assertTrue(callable(importlib.import_module(name).main), name)

    def test_use_cases_and_workflows_do_not_import_runtime_or_interfaces(self):
        forbidden = (
            "robert_agent.runtime",
            "robert_agent.interfaces",
            "robert_agent.cli",
            "robert_agent.acceptance",
            "robert_agent.worker",
        )
        for name, dependencies in self.graph.items():
            if ".application." in name or name.startswith("robert_agent.workflows."):
                for dependency in dependencies:
                    self.assertFalse(
                        any(dependency == item or dependency.startswith(item + ".") for item in forbidden),
                        (name, dependency),
                    )

    def test_runtime_and_http_do_not_execute_business_sql(self):
        paths = [*PACKAGE.joinpath("runtime").rglob("*.py"), *PACKAGE.joinpath("interfaces/web").rglob("*.py")]
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(
                        node.func.attr,
                        {"execute", "executemany", "executescript"},
                        (str(path.relative_to(PACKAGE)), node.lineno),
                    )


if __name__ == "__main__":
    unittest.main()

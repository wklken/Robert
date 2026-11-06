"""Keep migrated owners independent of orchestration and workflow implementations."""
import ast
from pathlib import Path
import unittest


PACKAGE = Path(__file__).resolve().parents[1] / 'src' / 'robert_agent'


def import_graph(package):
    modules = {}
    for path in package.rglob('*.py'):
        parts = path.relative_to(package).with_suffix('').parts
        if parts[-1] == '__init__':
            parts = parts[:-1]
        modules['.'.join(('robert_agent', *parts))] = path
    graph = {}
    for name, path in modules.items():
        dependencies = set()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names if alias.name in modules)
            elif isinstance(node, ast.ImportFrom):
                parent = node.module or ''
                if node.level:
                    package_name = name if path.name == '__init__.py' else name.rsplit('.', 1)[0]
                    prefix = package_name.split('.')[:len(package_name.split('.')) - node.level + 1]
                    parent = '.'.join([*prefix, *([parent] if parent else [])])
                if parent in modules:
                    dependencies.add(parent)
                dependencies.update(f'{parent}.{alias.name}' for alias in node.names
                                    if f'{parent}.{alias.name}' in modules)
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
            self.assertTrue(ready, 'cyclic source imports: ' + ', '.join(sorted(remaining)))
            remaining.difference_update(ready)

    def test_configuration_imports_only_configuration_and_boundary_capabilities(self):
        allowed = {'robert_agent', 'robert_agent.configuration', 'robert_agent.resource_files',
                   'robert_agent.worker_adapters'}
        allowed.update(name for name in self.graph if name.startswith('robert_agent.configuration.'))
        for name in sorted(self.graph):
            if name.startswith('robert_agent.configuration.'):
                with self.subTest(module=name):
                    self.assert_dependencies(name, allowed)

    def test_pure_policies_have_no_application_dependencies(self):
        for name in ('work.policies', 'delivery.policies'):
            with self.subTest(module=name):
                self.assert_dependencies('robert_agent.' + name, ())

    def test_leaf_capabilities_do_not_import_their_callers(self):
        allowed = {
            'adapters.processes.attempt': {'robert_agent', 'robert_agent.worker_identity'},
            'adapters.git.remotes': set(),
            'adapters.github.client': {'robert_agent.common'},
            'adapters.github.sources': {'robert_agent.adapters.github', 'robert_agent.adapters.github.client'},
            'adapters.github.pull_requests': set(),
            'security.text': {'robert_agent.redaction'},
            'work.queries': set(),
            'work.repositories': set(),
            'execution.leases': {'robert_agent', 'robert_agent.worker_identity'},
            'execution.notifications': set(),
            'execution.ownership': {'robert_agent', 'robert_agent.execution',
                'robert_agent.execution.notifications', 'robert_agent.execution_store',
                'robert_agent.execution_operations'},
        }
        for name, dependencies in allowed.items():
            with self.subTest(module=name):
                self.assert_dependencies('robert_agent.' + name, dependencies)

    def test_submission_and_stages_do_not_load_workflow_implementations(self):
        self.assert_dependencies('robert_agent.result_contract',
                                 {'robert_agent.delivery', 'robert_agent.delivery.policies'})
        self.assert_dependencies('robert_agent.execution.stages', {
            'robert_agent', 'robert_agent.dispatch', 'robert_agent.execution_store',
            'robert_agent.model_environment', 'robert_agent.retry_policy',
            'robert_agent.worker_identity', 'robert_agent.publication_lock',
            'robert_agent.worker', 'robert_agent.worker.snapshot',
        })

    def test_migrated_consumers_do_not_borrow_runtime_or_view_functions(self):
        # The paused monitor still shares run_once's monitor; moving the whole
        # recovery use case is a later unit, not a permissive package-wide waiver.
        consumers = ('work_items', 'work_item_control', 'execution_replay', 'github_scan',
                     'publish', 'supervision_publication', 'control_lane', 'result_cutover')
        forbidden = {'robert_agent.run_once', 'robert_agent.daemon', 'robert_agent.board_execution'}
        for name in consumers:
            with self.subTest(module=name):
                self.assertEqual(self.graph['robert_agent.' + name] & forbidden, set())
        for name, dependencies in self.graph.items():
            if name.startswith(('robert_agent.work.', 'robert_agent.execution.',
                                'robert_agent.configuration.', 'robert_agent.delivery.',
                                'robert_agent.adapters.', 'robert_agent.security.')):
                with self.subTest(module=name):
                    self.assertEqual(dependencies & forbidden, set())


if __name__ == '__main__':
    unittest.main()

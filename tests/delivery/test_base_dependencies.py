"""Dependency certificates reject ambiguous Python import behavior explicitly."""

import unittest

from robert_agent.delivery.base_dependencies import IncompleteDependencies, closed_dependencies


class ClosedDependencyTests(unittest.TestCase):
    def test_imported_symbols_must_exist(self):
        with self.assertRaisesRegex(IncompleteDependencies, "symbol does not exist"):
            closed_dependencies({"consumer.py": "from source import missing\n", "source.py": "value = 1\n"})

    def test_cycles_are_explicitly_refused(self):
        with self.assertRaisesRegex(IncompleteDependencies, "dependency cycle"):
            closed_dependencies({"left.py": "import right\n", "right.py": "import left\n"})

    def test_transitive_reexports_are_resolved_before_certifying(self):
        proof = closed_dependencies(
            {
                "consumer.py": "from middle import renamed\n",
                "middle.py": "from source import value as renamed\n",
                "source.py": "value = 1\n",
            }
        )
        self.assertEqual(
            proof["dependencies"],
            {
                "consumer.py": ["middle.py"],
                "middle.py": ["source.py"],
                "source.py": [],
            },
        )

    def test_module_metadata_and_stdlib_shadowing_are_outside_certificate(self):
        for sources in ({"os.py": "value = 1\n"}, {"source.py": "__path__ = 'runtime'\n"}):
            with self.subTest(sources=sources):
                with self.assertRaises(IncompleteDependencies):
                    closed_dependencies(sources)

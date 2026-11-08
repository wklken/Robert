import sys
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.configuration.supervision import normalize


class DecisionConfigurationTests(unittest.TestCase):
    def test_decision_authority_requires_an_explicit_profile(self):
        with self.assertRaises(ValueError):
            normalize({"mode": "decide"})
        settings = normalize({"mode": "decide", "decision": {"command": [sys.executable]}})
        self.assertEqual(settings["mode"], "decide")
        self.assertEqual(settings["decision"]["max_calls_per_hour"], 6)

    def test_diagnosis_does_not_implicitly_enable_decisions(self):
        settings = normalize({"diagnosis": {"command": [sys.executable]}})
        self.assertEqual(settings["mode"], "observe")
        self.assertIsNone(settings.get("decision"))

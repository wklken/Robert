"""The one-pass repair prompt discloses unresolved findings without another review."""

import unittest

from robert_agent.context.native_prompts import address_findings_input
from robert_agent.context.prompts import render_prompt


class SingleRoundPromptTests(unittest.TestCase):
    def request(self):
        return address_findings_input(
            {"head_sha": "a" * 40}, "/candidate.json", "/report.md", "report-digest", ["F1", "F2"]
        )

    def test_single_round_resource_explains_delivery_and_retains_required_checks(self):
        prompt = render_prompt(self.request(), stage_version=2)
        self.assertIn("exactly one repair pass", prompt.text)
        self.assertIn("No second model review or repair pass", prompt.text)
        self.assertIn("needs_decision", prompt.text)
        self.assertIn("remaining risk", prompt.text)
        self.assertIn("every required final gate must pass", prompt.text)
        self.assertNotIn("include a top-level choices list", prompt.text)
        self.assertNotIn("Unresolved decisions need answer choices", prompt.text)
        self.assertIn("stages/review-address-findings-single-round-v2.md", prompt.metadata["resources"])

    def test_historical_resource_keeps_the_frozen_decision_contract(self):
        current = render_prompt(self.request())
        historical = render_prompt(self.request(), stage_version=1)
        self.assertEqual(historical.text, current.text)
        self.assertIn("include a top-level choices list", historical.text)
        self.assertIn("single bounded targeted repair", historical.text)
        self.assertNotIn("single-round-v2.md", str(historical.metadata["resources"]))

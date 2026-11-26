"""PR review feedback is separate from the request's execution authority."""

import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.foundation import review_recipient


class ReviewRecipientTests(unittest.TestCase):
    def test_human_pr_author_receives_decisions_without_changing_request_owner(self):
        owner = {"login": "requester", "source_key": "github:x/y!1", "event_fingerprint": "review_request:1"}
        self.assertEqual(review_recipient.decision_owner(owner, "RimuruChan", "robert-bot"), {"login": "RimuruChan"})
        self.assertEqual(owner["login"], "requester")

    def test_robot_pr_preserves_original_owner_case_insensitively(self):
        owner = {"login": "Alice"}
        self.assertIs(review_recipient.decision_owner(owner, "Robert-Bot", "robert-bot"), owner)
        self.assertIsNone(review_recipient.decision_owner(None, "Robert-Bot", "robert-bot"))

    def test_missing_or_invalid_author_does_not_substitute_requester(self):
        for author in (None, "", "not a login", "github"):
            with self.subTest(author=author):
                self.assertIsNone(review_recipient.decision_owner({"login": "requester"}, author, "robert-bot"))

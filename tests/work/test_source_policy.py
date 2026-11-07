import unittest

from robert_agent.work import source_policy


class SourcePolicyTests(unittest.TestCase):
    def test_saved_notification_requires_current_trust_and_complete_lookup(self):
        saved = {
            "event_type": "notification",
            "actor_login": "author",
            "requested_reviewer": "robot",
            "requester_login": "reviewer",
            "authorization_lookup_complete": True,
            "trusted_trigger_found": True,
        }
        for actors, lookup, status, drives_execution in (
            (["reviewer"], True, "authorized_trigger", True),
            ([], True, "ignored_untrusted_trigger", False),
            (["reviewer"], False, "pending_authorization", False),
        ):
            with self.subTest(actors=actors, lookup=lookup):
                event = {**saved, "authorization_lookup_complete": lookup}
                decision = source_policy.reauthorize_saved_request(
                    event, {"github_account": "robot", "trusted_actors": actors}
                )
                self.assertEqual(decision["authorization_status"], status)
                self.assertEqual(decision["drives_execution"], drives_execution)
                self.assertTrue(event["trusted_trigger_found"])

    def test_saved_comment_loses_authority_when_the_bot_mention_changes(self):
        decision = source_policy.reauthorize_saved_request(
            {
                "event_type": "comment",
                "actor_login": "maintainer",
                "body": "@previous-bot investigate",
                "mentions_dd": True,
                "workstream_id": "work-1",
            },
            {"github_account": "robot", "trusted_actors": ["maintainer"]},
            known_workstreams={"work-1"},
        )

        self.assertEqual(decision["authorization_status"], "ignored_context")
        self.assertFalse(decision["drives_execution"])

    def test_refresh_keeps_fresh_event_and_only_covered_pending_sources(self):
        fresh = {
            "source_type": "issue",
            "number": 2,
            "event_fingerprint": "comment:2",
            "body": "fresh evidence",
        }
        stale = {**fresh, "body": "stale evidence"}
        pending = {
            "source_type": "pull_request",
            "number": 2,
            "event_fingerprint": "notification:3",
            "event_type": "notification",
            "requested_reviewer": "previous-bot",
            "requester_login": "previous-maintainer",
            "authorization_lookup_complete": True,
            "mentions_dd": True,
            "trusted_trigger_found": True,
        }
        uncovered = {**pending, "number": 4, "event_fingerprint": "notification:4"}

        refreshed = source_policy.refresh_pending_events(
            [fresh],
            [stale, pending, uncovered],
            {("issue", 2), ("pull_request", 2)},
            {"github_account": "robot", "trusted_actors": ["maintainer"]},
        )

        self.assertEqual(
            [event["event_fingerprint"] for event in refreshed],
            ["comment:2", "notification:3"],
        )
        self.assertEqual(refreshed[0]["body"], "fresh evidence")
        self.assertFalse(refreshed[1]["mentions_dd"])
        self.assertFalse(refreshed[1]["trusted_trigger_found"])
        self.assertTrue(pending["trusted_trigger_found"])

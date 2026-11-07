import unittest
REPO_CONFIG = {
    "full_name": "example/backend",
    "github_account": "robert-bot",
    "trusted_actors": ["wklken"],
}


class _Completed:
    def __init__(self, args, stdout):
        self.args = args
        self.stdout = stdout
        self.returncode = 0


class EventNormalizationTests(unittest.TestCase):
    def normalize_and_authorize(self, raw_events, existing_workstreams=None):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        return authorize.authorize_events(
            events,
            REPO_CONFIG,
            known_workstreams=existing_workstreams or set(),
        )

    def test_normalize_requires_event_id_without_explicit_fingerprint(self):
        from robert_agent.adapters.github import discovery as discover
        with self.assertRaisesRegex(ValueError, "event id"):
            discover.normalize_events(
                [
                    {
                        "number": 123,
                        "source_type": "issue",
                        "event_type": "comment",
                        "actor_login": "wklken",
                        "body": "@robert-bot please fix this",
                    }
                ],
                REPO_CONFIG,
            )

    def test_normalize_allows_explicit_fingerprint_without_event_id(self):
        from robert_agent.adapters.github import discovery as discover
        events = discover.normalize_events(
            [
                {
                    "number": 123,
                    "source_type": "issue",
                    "event_type": "comment",
                    "event_fingerprint": "comment:fixture-123-a",
                    "actor_login": "wklken",
                    "body": "@robert-bot please fix this",
                }
            ],
            REPO_CONFIG,
        )

        self.assertEqual(events[0]["event_fingerprint"], "comment:fixture-123-a")

    def test_fixture_pr_body_extracts_dd_origin_workstream_without_live_lookup(self):
        from robert_agent.adapters.github import discovery as discover
        events = discover.normalize_events(
            [
                {
                    "id": "review-701",
                    "number": 701,
                    "source_type": "pull_request",
                    "event_type": "review",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "body": "<!-- robert-workstream\nworkstream_id: github:example/backend#123\ntask_id: task-parent\ncreated_by: robert\n-->",
                }
            ],
            REPO_CONFIG,
        )

        self.assertEqual(events[0]["workstream_id"], "github:example/backend!701")
        self.assertEqual(events[0]["origin_workstream_id"], "github:example/backend#123")
        self.assertTrue(events[0]["has_open_dd_pr"])
        self.assertEqual(events[0]["metadata"]["dd_workstream"]["task_id"], "task-parent")

    def test_legacy_dd_workstream_metadata_maps_to_origin_workstream_id(self):
        from robert_agent.adapters.github import discovery as discover
        events = discover.normalize_events(
            [
                {
                    "id": "review-701",
                    "number": 701,
                    "source_type": "pull_request",
                    "event_type": "review",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "body": "<!-- robert-workstream\nworkstream_id: github:example/backend#123\ntask_id: task-parent\ncreated_by: robert\n-->",
                }
            ],
            REPO_CONFIG,
        )

        self.assertEqual(events[0]["workstream_id"], "github:example/backend!701")
        self.assertEqual(events[0]["origin_workstream_id"], "github:example/backend#123")

    def test_dd_pr_event_keeps_pr_workstream_and_tracks_origin_issue(self):
        from robert_agent.adapters.github import discovery as discover
        events = discover.normalize_events(
            [
                {
                    "id": "comment-1",
                    "number": 456,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "wklken",
                    "author_association": "COLLABORATOR",
                    "body": "@robert-bot follow up",
                    "metadata": {
                        "dd_workstream": {
                            "workstream_id": "github:example/backend#123",
                            "source_issue": "123",
                        }
                    },
                }
            ],
            REPO_CONFIG,
        )

        self.assertEqual(events[0]["workstream_id"], "github:example/backend!456")
        self.assertEqual(events[0]["origin_workstream_id"], "github:example/backend#123")

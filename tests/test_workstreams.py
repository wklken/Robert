import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class WorkstreamTests(unittest.TestCase):
    def test_issue_event_creates_workstream_from_source_key(self):
        from robert_agent import workstream
        event = {
            "source_key": "github:example/backend#123",
            "source_type": "issue",
            "event_fingerprint": "comment:1",
        }

        decision = workstream.plan_event(event, active_workstreams=set())

        self.assertEqual(decision["action"], "create_task")
        self.assertEqual(decision["workstream_id"], event["source_key"])
        self.assertEqual(decision["pending_events"], [])

    def test_dd_pr_metadata_tracks_origin_but_stays_on_pr_workstream(self):
        from robert_agent import workstream
        event = {
            "source_key": "github:example/backend!456",
            "source_type": "pull_request",
            "event_fingerprint": "review:1",
            "workstream_id": "github:example/backend!456",
            "origin_workstream_id": "github:example/backend#123",
            "metadata": {
                "dd_workstream": {
                    "workstream_id": "github:example/backend#123"
                }
            },
        }

        decision = workstream.plan_event(event, active_workstreams=set())

        self.assertEqual(decision["workstream_id"], "github:example/backend!456")
        self.assertEqual(decision["source_relationship"], "derived_pr")

    def test_dd_pr_mainline_stays_on_pr_source_key(self):
        from robert_agent import workstream
        event = {
            "source_key": "github:example/backend!456",
            "source_type": "pull_request",
            "workstream_id": "github:example/backend!456",
            "origin_workstream_id": "github:example/backend#123",
            "event_fingerprint": "comment:1",
        }

        decision = workstream.plan_event(
            event,
            active_workstreams={"github:example/backend#123"},
        )

        self.assertEqual(decision["action"], "create_task")
        self.assertEqual(decision["workstream_id"], "github:example/backend!456")
        self.assertEqual(decision["source_relationship"], "derived_pr")

    def test_active_workstream_gets_pending_event_instead_of_new_task(self):
        from robert_agent import workstream
        event = {
            "source_key": "github:example/backend#123",
            "source_type": "issue",
            "event_fingerprint": "comment:2",
        }

        decision = workstream.plan_event(
            event,
            active_workstreams={"github:example/backend#123"},
        )

        self.assertEqual(decision["action"], "append_pending_event")
        self.assertEqual(decision["pending_events"], ["comment:2"])


if __name__ == "__main__":
    unittest.main()

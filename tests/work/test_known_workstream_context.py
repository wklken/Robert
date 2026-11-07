import json
from tests.support.temp_workspace import TemporaryWorkspace


class KnownWorkstreamContextTests(TemporaryWorkspace):
    def test_known_workstream_context_uses_latest_dd_mention_only(self):
        from robert_agent.adapters.github import discovery as discover

        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def runner(args, **_kwargs):
            if args[:3] == ["gh", "api", "repos/example/repo/issues/456/comments"]:
                return Completed(
                    json.dumps(
                        [
                            {
                                "id": "comment-mention",
                                "user": {"login": "wklken"},
                                "author_association": "OWNER",
                                "body": "@robot please retry the original task",
                                "created_at": "2026-06-23T01:00:00Z",
                            },
                            {
                                "id": "comment-no-mention",
                                "user": {"login": "wklken"},
                                "author_association": "OWNER",
                                "body": "this is just a status update",
                                "created_at": "2026-06-23T02:00:00Z",
                            },
                        ]
                    )
                )
            if args[:3] in [
                ["gh", "api", "repos/example/repo/pulls/456/reviews"],
                ["gh", "api", "repos/example/repo/pulls/456/comments"],
            ]:
                return Completed("[]")
            raise AssertionError(args)

        raw = {
            "id": "notification-1",
            "number": 456,
            "source_type": "pull_request",
            "event_type": "notification",
            "actor_login": "github",
        }
        result = discover._enrich_known_workstream_context(
            raw,
            {"full_name": "example/repo", "github_account": "robot"},
            runner,
        )

        self.assertEqual(result["id"], "comment-mention")
        self.assertEqual(result["event_fingerprint"], "comment:comment-mention")
        self.assertIn("@robot", result["body"])

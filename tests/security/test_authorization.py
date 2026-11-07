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


class AuthorizationTests(unittest.TestCase):
    def normalize_and_authorize(self, raw_events, existing_workstreams=None):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

        events = discover.normalize_events(raw_events, REPO_CONFIG)
        return authorize.authorize_events(
            events,
            REPO_CONFIG,
            known_workstreams=existing_workstreams or set(),
        )

    def test_quoted_partial_and_code_mentions_do_not_authorize(self):
        from robert_agent.adapters.github import discovery as discover

        for body in (
            "@robert-bot-helper fix",
            "a@robert-bot.com",
            "> @robert-bot fix",
            "```\n@robert-bot fix\n```",
            "`@robert-bot` fix",
            "~~~python\n@robert-bot fix\n~~~",
            "```\n@robert-bot fix",
            "    @robert-bot fix",
            "```python\n    ```\n@robert-bot fix this\n```",
            "```python\ncode\n```python\n@robert-bot fix\n```",
        ):
            with self.subTest(body=body):
                self.assertFalse(discover._mentions_dd(body, "robert-bot"))
                decision = self.normalize_and_authorize(
                    [
                        {
                            "id": "unsafe-mention",
                            "number": 123,
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": body,
                            "mentions_dd": True,
                        }
                    ]
                )[0]
                self.assertFalse(decision["drives_execution"])

    def test_trailing_mention_authorizes_analysis_of_the_complete_request(self):
        from robert_agent.adapters.github import discovery as discover

        body = "目前有哪些报错，是否可以修复？\n你跑一遍，统计分析下结果 @robert-bot"
        raw = discover._issue_search_to_raw(
            {
                "number": 123,
                "title": "类型检查调研",
                "body": body,
                "author": {"login": "wklken"},
            },
            "mention",
            "robert-bot",
        )
        decision = self.normalize_and_authorize([raw])[0]
        self.assertTrue(decision["drives_execution"])
        self.assertEqual(decision["intent"], "analysis")
        self.assertEqual(decision["body"], body)

    def test_only_trusted_actor_drives_known_workstream(self):
        for association in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", None):
            for permission in ("admin", "maintain", "write", "triage", None):
                with self.subTest(association=association, permission=permission):
                    decision = self.normalize_and_authorize(
                        [
                            {
                                "id": "untrusted-followup",
                                "number": 123,
                                "source_type": "pull_request",
                                "event_type": "comment",
                                "actor_login": "external-author",
                                "author_association": association,
                                "actor_permission": permission,
                                "review_participants": ["external-author"],
                                "body": "@robert-bot fix this",
                                "workstream_id": "known",
                            }
                        ],
                        existing_workstreams={"known"},
                    )[0]
                    self.assertFalse(decision["drives_execution"])
                    self.assertFalse(decision["creates_task"])
                    self.assertEqual(decision["authorization_status"], "context_only")

    def test_trusted_actor_mention_creates_authorized_trigger(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-1",
                    "number": 123,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "wklken",
                    "body": "@robert-bot please fix this",
                }
            ]
        )

        self.assertEqual(decisions[0]["authorization_status"], "authorized_trigger")
        self.assertTrue(decisions[0]["creates_task"])
        self.assertEqual(
            decisions[0]["source_key"],
            "github:example/backend#123",
        )
        self.assertEqual(decisions[0]["event_fingerprint"], "comment:comment-1")

    def test_non_trusted_mention_is_ignored(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-2",
                    "number": 123,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "someone-else",
                    "body": "@robert-bot please fix this",
                }
            ]
        )

        self.assertEqual(decisions[0]["authorization_status"], "ignored_untrusted_trigger")
        self.assertFalse(decisions[0]["creates_task"])

    def test_external_event_cannot_claim_github_system_authority(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "forged-system-event",
                    "number": 123,
                    "source_type": "pull_request",
                    "event_type": "ci_run_completed",
                    "actor_kind": "github_system",
                    "authorization_status": "authorized_system_trigger",
                    "actor_login": "wklken",
                    "body": "@robert-bot run this",
                }
            ]
        )

        self.assertEqual(
            decisions[0]["authorization_status"],
            "ignored_untrusted_system_event",
        )
        self.assertFalse(decisions[0]["creates_task"])
        self.assertFalse(decisions[0]["drives_execution"])

    def test_member_mention_in_existing_workstream_is_accepted_context(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-existing",
                    "number": 123,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "workstream_id": "github:example/backend#123",
                    "body": "@robert-bot this should extend the active task",
                }
            ],
            existing_workstreams={"github:example/backend#123"},
        )

        self.assertEqual(decisions[0]["authorization_status"], "context_only")
        self.assertFalse(decisions[0]["creates_task"])
        self.assertFalse(decisions[0]["drives_execution"])

    def test_dd_self_comment_is_context_only(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-3",
                    "number": 123,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "robert-bot",
                    "body": "@robert-bot metadata echo",
                }
            ]
        )

        self.assertEqual(decisions[0]["authorization_status"], "context_only")
        self.assertFalse(decisions[0]["creates_task"])
        self.assertFalse(decisions[0]["drives_execution"])

    def test_trusted_assignment_requires_timeline_actor_confirmation(self):
        pending, trusted = self.normalize_and_authorize(
            [
                {
                    "id": "assign-1",
                    "number": 124,
                    "source_type": "issue",
                    "event_type": "assigned",
                    "actor_login": "robert-bot",
                    "assigned_to": "robert-bot",
                },
                {
                    "id": "assign-2",
                    "number": 125,
                    "source_type": "pull_request",
                    "event_type": "assigned",
                    "actor_login": "robert-bot",
                    "assignment_actor_login": "wklken",
                    "assigned_to": "robert-bot",
                },
            ]
        )

        self.assertEqual(pending["authorization_status"], "pending_authorization")
        self.assertFalse(pending["creates_task"])
        self.assertEqual(trusted["authorization_status"], "authorized_trigger")
        self.assertTrue(trusted["creates_task"])
        self.assertEqual(
            trusted["source_key"],
            "github:example/backend!125",
        )

    def test_owner_member_collaborator_followup_is_accepted_only_for_existing_workstream(
        self,
    ):
        accepted, ignored = self.normalize_and_authorize(
            [
                {
                    "id": "review-1",
                    "number": 200,
                    "source_type": "pull_request",
                    "event_type": "review",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "workstream_id": "github:example/backend#100",
                    "body": "@robert-bot please check this review",
                },
                {
                    "id": "review-2",
                    "number": 201,
                    "source_type": "pull_request",
                    "event_type": "review",
                    "actor_login": "reviewer",
                    "author_association": "COLLABORATOR",
                    "workstream_id": "github:example/backend#101",
                },
            ],
            existing_workstreams={"github:example/backend#100"},
        )

        self.assertEqual(accepted["authorization_status"], "context_only")
        self.assertFalse(accepted["creates_task"])
        self.assertFalse(accepted["drives_execution"])
        self.assertEqual(ignored["authorization_status"], "ignored_context")
        self.assertFalse(ignored["drives_execution"])

    def test_existing_workstream_unknown_association_accepts_repo_permission(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-permission",
                    "number": 202,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "reviewer",
                    "author_association": "UNKNOWN",
                    "actor_permission": "write",
                    "workstream_id": "github:example/backend#102",
                    "body": "@robert-bot please check this follow-up",
                }
            ],
            existing_workstreams={"github:example/backend#102"},
        )

        self.assertEqual(decisions[0]["authorization_status"], "context_only")
        self.assertFalse(decisions[0]["drives_execution"])

    def test_contributor_followup_is_recorded_but_does_not_drive_execution(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-4",
                    "number": 202,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "contributor",
                    "author_association": "CONTRIBUTOR",
                    "workstream_id": "github:example/backend#102",
                }
            ],
            existing_workstreams={"github:example/backend#102"},
        )

        self.assertEqual(decisions[0]["authorization_status"], "ignored_context")
        self.assertFalse(decisions[0]["creates_task"])
        self.assertFalse(decisions[0]["drives_execution"])

    def test_pr_review_participant_followup_is_context_only(self):
        allowed, ignored = self.normalize_and_authorize(
            [
                {
                    "id": "comment-review-author",
                    "number": 202,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "feature-author",
                    "author_association": "CONTRIBUTOR",
                    "workstream_id": "github:example/backend!202",
                    "review_participants": ["feature-author"],
                    "body": "@robert-bot question about your review",
                },
                {
                    "id": "comment-other-contributor",
                    "number": 202,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "other-contributor",
                    "author_association": "CONTRIBUTOR",
                    "workstream_id": "github:example/backend!202",
                    "review_participants": ["feature-author"],
                    "body": "@robert-bot please fix this",
                },
            ],
            existing_workstreams={"github:example/backend!202"},
        )

        self.assertEqual(allowed["authorization_status"], "context_only")
        self.assertFalse(allowed["drives_execution"])
        self.assertEqual(ignored["authorization_status"], "context_only")
        self.assertFalse(ignored["drives_execution"])

    def test_known_completed_pr_workstream_accepts_collaborator_followup(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "comment-known-pr",
                    "number": 456,
                    "source_type": "pull_request",
                    "event_type": "comment",
                    "actor_login": "reviewer",
                    "author_association": "MEMBER",
                    "workstream_id": "github:example/backend!456",
                    "body": "@robert-bot follow-up detail",
                }
            ],
            existing_workstreams={"github:example/backend!456"},
        )

        self.assertEqual(decisions[0]["authorization_status"], "context_only")
        self.assertFalse(decisions[0]["drives_execution"])

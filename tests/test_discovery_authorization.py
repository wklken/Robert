import subprocess
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


REPO_CONFIG = {
    "full_name": "example/backend",
    "github_account": "robert-bot",
    "trusted_actors": ["wklken"],
}


class DiscoveryAuthorizationTests(unittest.TestCase):
    def normalize_and_authorize(self, raw_events, existing_workstreams=None):
        from robert_agent import authorize
        from robert_agent import discover
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        return authorize.authorize_events(
            events,
            REPO_CONFIG,
            known_workstreams=existing_workstreams or set(),
        )

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

    def test_normalize_requires_event_id_without_explicit_fingerprint(self):
        from robert_agent import discover
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
        from robert_agent import discover
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

        self.assertEqual(decisions[0]["authorization_status"], "accepted_context")
        self.assertFalse(decisions[0]["creates_task"])
        self.assertTrue(decisions[0]["drives_execution"])

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

    def test_notification_without_trusted_trigger_is_ignored_after_lookup(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "notification-1",
                    "number": 126,
                    "source_type": "issue",
                    "event_type": "notification",
                    "actor_login": "github",
                    "trusted_trigger_found": False,
                    "authorization_lookup_complete": True,
                }
            ]
        )

        self.assertEqual(decisions[0]["authorization_status"], "ignored_untrusted_trigger")

    def test_notification_with_missing_timeline_stays_pending(self):
        decisions = self.normalize_and_authorize(
            [
                {
                    "id": "notification-2",
                    "number": 127,
                    "source_type": "issue",
                    "event_type": "notification",
                    "actor_login": "github",
                    "trusted_trigger_found": False,
                    "authorization_lookup_complete": False,
                }
            ]
        )

        self.assertEqual(decisions[0]["authorization_status"], "pending_authorization")

    def test_account_notifications_bucket_by_repository_full_name(self):
        from robert_agent import discover
        repo_a = {
            "full_name": "Org/repo-a",
            "github_account": "robert-bot",
            "trusted_actors": ["alice"],
        }
        repo_b = {
            "full_name": "Org/repo-b",
            "github_account": "robert-bot",
            "trusted_actors": ["bob"],
        }

        def fake_runner(args, **_kwargs):
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "thread-a",
                          "updated_at": "2026-07-04T01:00:00Z",
                          "repository": {"full_name": "Org/repo-a"},
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/Org/repo-a/issues/10"
                          }
                        },
                        {
                          "id": "thread-b",
                          "updated_at": "2026-07-04T01:01:00Z",
                          "repository": {"full_name": "Org/repo-b"},
                          "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/Org/repo-b/pulls/10"
                          }
                        },
                        {
                          "id": "thread-c",
                          "updated_at": "2026-07-04T01:02:00Z",
                          "repository": {"full_name": "Org/unconfigured"},
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/Org/unconfigured/issues/10"
                          }
                        }
                      ]
                    ]""",
                )
            raise AssertionError(args)

        buckets = discover.collect_account_notifications([repo_a, repo_b], runner=fake_runner)

        self.assertEqual(sorted(buckets), ["Org/repo-a", "Org/repo-b"])
        self.assertEqual(buckets["Org/repo-a"][0]["repo_full_name"], "Org/repo-a")
        self.assertEqual(buckets["Org/repo-a"][0]["number"], 10)
        self.assertEqual(buckets["Org/repo-b"][0]["repo_full_name"], "Org/repo-b")
        self.assertEqual(buckets["Org/repo-b"][0]["source_type"], "pull_request")

    def test_repo_live_events_accept_preloaded_notification_hints_without_refetching_notifications(self):
        from robert_agent import discover
        calls = []
        repo = {
            "full_name": "Org/repo-a",
            "github_account": "robert-bot",
            "trusted_actors": ["alice"],
        }
        notification_hints = [
            {
                "id": "thread-a",
                "number": 10,
                "source_type": "issue",
                "event_type": "notification",
                "actor_login": "github",
                "repo_full_name": "Org/repo-a",
                "trusted_trigger_found": False,
                "authorization_lookup_complete": False,
                "event_at": "2026-07-04T01:00:00Z",
            }
        ]

        def fake_runner(args, **_kwargs):
            calls.append(args)
            if "--assignee" in args or "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/Org/repo-a/issues/10"]:
                return _Completed(
                    args,
                    """{
                      "title": "Need work",
                      "state": "open",
                      "updated_at": "2026-07-04T01:00:00Z",
                      "html_url": "https://github.com/Org/repo-a/issues/10",
                      "user": {"login": "alice"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/Org/repo-a/issues/10/timeline"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "assign-a",
                        "event": "assigned",
                        "created_at": "2026-07-04T00:59:00Z",
                        "actor": {"login": "alice"},
                        "assignee": {"login": "robert-bot"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/Org/repo-a/issues/10/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-a",
                        "body": "@robert-bot please fix",
                        "created_at": "2026-07-04T01:00:00Z",
                        "user": {"login": "alice"},
                        "author_association": "MEMBER",
                        "html_url": "https://github.com/Org/repo-a/issues/10#issuecomment-comment-a"
                      }
                    ]""",
                )
            raise AssertionError(args)

        raw_events = discover.collect_live_events(
            repo,
            runner=fake_runner,
            notification_hints=notification_hints,
        )
        events = discover.normalize_events(raw_events, repo)

        self.assertEqual(events[0]["repo"], "Org/repo-a")
        self.assertEqual(events[0]["event_fingerprint"], "assigned:assign-a")
        self.assertEqual(events[0]["assignment_actor_login"], "alice")
        self.assertNotIn(["gh", "api", "notifications", "--paginate", "--slurp"], calls)

    def test_repo_live_events_ignores_mismatched_preloaded_notification_hints(self):
        from robert_agent import discover
        calls = []
        repo = {
            "full_name": "Org/repo-a",
            "github_account": "robert-bot",
            "trusted_actors": ["alice"],
        }
        notification_hints = [
            {
                "id": "thread-b",
                "number": 10,
                "source_type": "issue",
                "event_type": "notification",
                "actor_login": "github",
                "repo_full_name": "Org/repo-b",
                "trusted_trigger_found": False,
                "authorization_lookup_complete": False,
                "event_at": "2026-07-04T01:00:00Z",
            }
        ]

        def fake_runner(args, **_kwargs):
            calls.append(args)
            if "--assignee" in args or "--mentions" in args:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(
            repo,
            runner=fake_runner,
            notification_hints=notification_hints,
        )

        self.assertEqual(raw_events, [])
        self.assertFalse(any("repos/Org/repo-a/issues/10" in " ".join(args) for args in calls))

    def test_owner_member_collaborator_followup_is_accepted_only_for_existing_workstream(self):
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

        self.assertEqual(accepted["authorization_status"], "accepted_context")
        self.assertFalse(accepted["creates_task"])
        self.assertTrue(accepted["drives_execution"])
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

        self.assertEqual(decisions[0]["authorization_status"], "accepted_context")
        self.assertTrue(decisions[0]["drives_execution"])

    def test_live_unknown_association_is_enriched_with_repo_permission(self):
        from robert_agent import authorize
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 777,
                        "title": "Follow-up",
                        "body": "@robert-bot please check this",
                        "isPullRequest": false,
                        "author": {"login": "reviewer"},
                        "authorAssociation": "UNKNOWN",
                        "updatedAt": "2026-06-16T00:00:00Z",
                        "url": "https://github.com/example/backend/issues/777"
                      }
                    ]""",
                )
            if args[:3] == [
                "gh",
                "api",
                "repos/example/backend/collaborators/reviewer/permission",
            ]:
                return _Completed(args, """{"permission": "write"}""")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/777/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        decisions = authorize.authorize_events(
            events,
            REPO_CONFIG,
            existing_workstreams={"github:example/backend#777"},
        )

        self.assertEqual(events[0]["actor_permission"], "write")
        self.assertEqual(decisions[0]["authorization_status"], "accepted_context")

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

    def test_pr_review_participant_followup_drives_only_scoped_context(self):
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

        self.assertEqual(allowed["authorization_status"], "accepted_review_participant")
        self.assertTrue(allowed["drives_execution"])
        self.assertEqual(ignored["authorization_status"], "ignored_context")
        self.assertFalse(ignored["drives_execution"])

    def test_live_discovery_uses_gh_search_and_notification_hints(self):
        from robert_agent import discover
        calls = []

        def fake_runner(args, **_kwargs):
            calls.append(args)
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 321,
                        "title": "Fix timeout",
                        "body": "@robert-bot please fix",
                        "isPullRequest": false,
                        "author": {"login": "wklken"},
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-06-16T00:00:00Z",
                        "url": "https://github.com/example/backend/issues/321"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/321/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "n1",
                          "updated_at": "2026-06-16T00:01:00Z",
                          "repository": {
                            "full_name": "example/backend"
                          },
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/322"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/322/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/322/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/322"]:
                return _Completed(
                    args,
                    """{
                      "number": 322,
                      "state": "open",
                      "title": "Notification hint",
                      "updated_at": "2026-06-16T00:01:00Z",
                      "html_url": "https://github.com/example/backend/issues/322"
                    }""",
                )
            raise AssertionError(args)

        events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)

        self.assertTrue(any("--mentions" in call for call in calls))
        self.assertTrue(any(call[:3] == ["gh", "api", "notifications"] for call in calls))
        self.assertEqual(events[0]["event_type"], "mention")
        self.assertEqual(events[0]["actor_login"], "wklken")
        self.assertEqual(events[1]["event_type"], "notification")
        self.assertEqual(events[1]["number"], 322)
        self.assertEqual(events[1]["state"], "open")

    def test_closed_issue_notification_hint_is_ignored(self):
        from robert_agent import discover
        calls = []

        def fake_runner(args, **_kwargs):
            calls.append(args)
            if "--assignee" in args or "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "n-closed-2878",
                          "updated_at": "2026-06-22T12:20:00Z",
                          "repository": {
                            "full_name": "example/backend"
                          },
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/2878"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/2878"]:
                return _Completed(
                    args,
                    """{
                      "number": 2878,
                      "state": "closed",
                      "state_reason": "completed",
                      "title": "分析 mcp-proxy 项目",
                      "updated_at": "2026-06-22T12:14:00Z",
                      "closed_at": "2026-06-22T12:14:00Z",
                      "html_url": "https://github.com/example/backend/issues/2878"
                    }""",
                )
            raise AssertionError(args)

        events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)

        self.assertEqual(events, [])
        self.assertFalse(
            any(
                call[:3] == [
                    "gh",
                    "api",
                    "repos/example/backend/issues/2878/comments",
                ]
                for call in calls
            )
        )

    def test_live_assignment_is_enriched_from_timeline_actor(self):
        from robert_agent import authorize
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 401,
                        "title": "Assigned bug",
                        "body": "",
                        "isPullRequest": false,
                        "author": {"login": "reporter"},
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-06-16T00:00:00Z",
                        "url": "https://github.com/example/backend/issues/401"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/401/timeline"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "assign-event-1",
                        "event": "assigned",
                        "created_at": "2026-06-16T00:01:00Z",
                        "actor": {"login": "wklken"},
                        "assignee": {"login": "robert-bot"}
                      }
                    ]""",
                )
            if "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        decisions = authorize.authorize_events(events, REPO_CONFIG, existing_workstreams=set())

        self.assertEqual(events[0]["assignment_actor_login"], "wklken")
        self.assertEqual(events[0]["assigned_to"], "robert-bot")
        self.assertEqual(events[0]["event_fingerprint"], "assigned:assign-event-1")
        self.assertEqual(decisions[0]["authorization_status"], "authorized_trigger")

    def test_notification_assignment_reuses_assignment_fingerprint(self):
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 2901,
                        "title": "Assigned bug",
                        "body": "@robert-bot please fix",
                        "isPullRequest": false,
                        "author": {"login": "reporter"},
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-06-22T12:40:00Z",
                        "url": "https://github.com/example/backend/issues/2901"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/2901/timeline"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "assign-event-2901",
                        "event": "assigned",
                        "created_at": "2026-06-22T12:41:00Z",
                        "actor": {"login": "wklken"},
                        "assignee": {"login": "robert-bot"}
                      }
                    ]""",
                )
            if "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-2901",
                          "updated_at": "2026-06-22T12:42:00Z",
                          "repository": {
                            "full_name": "example/backend"
                          },
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/2901"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/2901"]:
                return _Completed(
                    args,
                    """{
                      "number": 2901,
                      "state": "open",
                      "title": "Assigned bug",
                      "updated_at": "2026-06-22T12:42:00Z",
                      "html_url": "https://github.com/example/backend/issues/2901"
                    }""",
                )
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)

        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event["event_fingerprint"] for event in events],
            ["assigned:assign-event-2901", "assigned:assign-event-2901"],
        )
        self.assertEqual({event["event_type"] for event in events}, {"assigned", "notification"})

    def test_live_assignment_without_timeline_actor_stays_pending(self):
        from robert_agent import authorize
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 402,
                        "title": "Assigned without timeline",
                        "body": "",
                        "isPullRequest": false,
                        "author": {"login": "reporter"},
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-06-16T00:00:00Z",
                        "url": "https://github.com/example/backend/issues/402"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/402/timeline"]:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        decisions = authorize.authorize_events(events, REPO_CONFIG, existing_workstreams=set())

        self.assertEqual(decisions[0]["authorization_status"], "pending_authorization")

    def test_live_mention_prefers_trusted_comment_over_untrusted_body(self):
        from robert_agent import authorize
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 501,
                        "title": "Needs attention",
                        "body": "@robert-bot from untrusted issue body",
                        "isPullRequest": false,
                        "author": {"login": "someone-else"},
                        "authorAssociation": "CONTRIBUTOR",
                        "updatedAt": "2026-06-16T00:00:00Z",
                        "url": "https://github.com/example/backend/issues/501"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/501/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "trusted-comment-1",
                        "body": "@robert-bot please handle this",
                        "created_at": "2026-06-16T00:01:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        decisions = authorize.authorize_events(events, REPO_CONFIG, existing_workstreams=set())

        self.assertEqual(events[0]["actor_login"], "wklken")
        self.assertEqual(events[0]["event_fingerprint"], "comment:trusted-comment-1")
        self.assertEqual(decisions[0]["authorization_status"], "authorized_trigger")

    def test_live_pr_followup_extracts_dd_origin_workstream_and_head_branch(self):
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 601,
                        "title": "Follow-up review",
                        "body": "@robert-bot please fix this regression",
                        "isPullRequest": true,
                        "author": {"login": "wklken"},
                        "authorAssociation": "MEMBER",
                        "updatedAt": "2026-06-16T00:00:00Z",
                        "url": "https://github.com/example/backend/pull/601"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/601"]:
                return _Completed(
                    args,
                    """{
                      "body": "<!-- robert-workstream\\nworkstream_id: github:example/backend#123\\ntask_id: task-parent\\ncreated_by: robert\\n-->",
                      "head": {"ref": "codex/dd-123-fix-timeout"}
                    }""",
                )
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)

        self.assertEqual(events[0]["workstream_id"], "github:example/backend!601")
        self.assertEqual(events[0]["origin_workstream_id"], "github:example/backend#123")
        self.assertTrue(events[0]["has_open_dd_pr"])
        self.assertEqual(events[0]["existing_pr_head_branch"], "codex/dd-123-fix-timeout")
        self.assertEqual(events[0]["metadata"]["dd_workstream"]["task_id"], "task-parent")

    def test_live_pr_followup_uses_latest_trusted_comment(self):
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 603,
                        "title": "Follow-up review",
                        "body": "## Summary\\n- keep improving logs",
                        "isPullRequest": true,
                        "author": {"login": "robert-bot"},
                        "authorAssociation": "COLLABORATOR",
                        "updatedAt": "2026-06-17T09:53:31Z",
                        "url": "https://github.com/example/backend/pull/603"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/603"]:
                return _Completed(
                    args,
                    """{
                      "body": "<!-- robert-workstream\\norigin_workstream_id: github:example/backend#2884\\nsource_issue: 2884\\ntask_id: task-parent\\ncreated_by: robert\\n-->",
                      "head": {"ref": "codex/issue-2884-fix-operator"},
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/603/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-old",
                        "body": "@robert-bot old follow-up",
                        "created_at": "2026-06-17T07:03:46Z",
                        "author_association": "COLLABORATOR",
                        "user": {"login": "wklken"}
                      },
                      {
                        "id": "comment-new",
                        "body": "@robert-bot handle the latest review feedback",
                        "created_at": "2026-06-17T09:53:31Z",
                        "author_association": "COLLABORATOR",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/603/reviews"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/603/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)

        self.assertEqual(events[0]["event_fingerprint"], "comment:comment-new")
        self.assertEqual(events[0]["body"], "@robert-bot handle the latest review feedback")
        self.assertEqual(events[0]["workstream_id"], "github:example/backend!603")
        self.assertEqual(events[0]["existing_pr_head_branch"], "codex/issue-2884-fix-operator")

    def test_live_known_pr_workstream_accepts_collaborator_followup(self):
        from robert_agent import authorize
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 604,
                        "title": "Review feedback",
                        "body": "## Summary\\n- improve release logs",
                        "isPullRequest": true,
                        "author": {"login": "robert-bot"},
                        "authorAssociation": "COLLABORATOR",
                        "updatedAt": "2026-06-17T10:00:00Z",
                        "url": "https://github.com/example/backend/pull/604"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/604"]:
                return _Completed(
                    args,
                    """{
                      "body": "<!-- robert-workstream\\norigin_workstream_id: github:example/backend#2884\\nsource_issue: 2884\\ntask_id: task-parent\\ncreated_by: robert\\n-->",
                      "head": {"ref": "codex/issue-2884-fix-operator"},
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/604/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-member",
                        "body": "@robert-bot please handle the major review items",
                        "created_at": "2026-06-17T10:00:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/604/reviews"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/604/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(
            REPO_CONFIG,
            runner=fake_runner,
            known_workstreams={"github:example/backend!604"},
        )
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        decisions = authorize.authorize_events(
            events,
            REPO_CONFIG,
            existing_workstreams={"github:example/backend!604"},
        )

        self.assertEqual(events[0]["event_fingerprint"], "comment:comment-member")
        self.assertEqual(decisions[0]["authorization_status"], "accepted_context")
        self.assertTrue(decisions[0]["drives_execution"])

    def test_notification_for_known_pr_workstream_uses_latest_non_bot_comment(self):
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args or "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "n-pr-followup",
                          "updated_at": "2026-06-17T09:53:53Z",
                          "repository": {
                            "full_name": "example/backend"
                          },
                          "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/example/backend/pulls/2886"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/2886"]:
                return _Completed(
                    args,
                    """{
                      "body": "<!-- robert-workstream\\norigin_workstream_id: github:example/backend#2884\\nsource_issue: 2884\\ntask_id: task-parent\\ncreated_by: robert\\n-->",
                      "head": {"ref": "codex/issue-2884-fix-operator"},
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/2886"]:
                return _Completed(
                    args,
                    """{
                      "number": 2886,
                      "state": "open",
                      "title": "PR follow-up",
                      "updated_at": "2026-06-17T09:53:53Z",
                      "html_url": "https://github.com/example/backend/pull/2886"
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/2886/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/2886/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "old-comment",
                        "body": "@robert-bot earlier question",
                        "created_at": "2026-06-17T07:03:46Z",
                        "author_association": "COLLABORATOR",
                        "user": {"login": "wklken"}
                      },
                      {
                        "id": "bot-comment",
                        "body": "@wklken ack",
                        "created_at": "2026-06-17T07:23:09Z",
                        "author_association": "COLLABORATOR",
                        "user": {"login": "robert-bot"}
                      },
                      {
                        "id": "latest-comment",
                        "body": "@robert-bot handle the Major 3 items",
                        "created_at": "2026-06-17T09:53:31Z",
                        "author_association": "COLLABORATOR",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/2886/reviews"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/2886/comments"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(
            REPO_CONFIG,
            runner=fake_runner,
            known_workstreams={"github:example/backend!2886"},
        )
        events = discover.normalize_events(raw_events, REPO_CONFIG)

        self.assertEqual(events[0]["event_fingerprint"], "comment:latest-comment")
        self.assertEqual(events[0]["actor_login"], "wklken")
        self.assertEqual(events[0]["workstream_id"], "github:example/backend!2886")

    def test_live_robot_authored_pr_without_hidden_metadata_still_routes_to_existing_pr_update(self):
        from robert_agent import discover
        from robert_agent import route
        def fake_runner(args, **_kwargs):
            if "--assignee" in args:
                return _Completed(args, "[]")
            if "--mentions" in args:
                return _Completed(
                    args,
                    """[
                      {
                        "number": 602,
                        "title": "Correct version metadata",
                        "body": "@robert-bot please handle this follow-up",
                        "isPullRequest": true,
                        "author": {"login": "wklken"},
                        "authorAssociation": "COLLABORATOR",
                        "updatedAt": "2026-06-17T02:38:44Z",
                        "url": "https://github.com/example/backend/pull/602"
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/602"]:
                return _Completed(
                    args,
                    """{
                      "body": "## Summary\\n- update AGENTS wording\\n\\nRefs #2878",
                      "head": {"ref": "codex/issue-2878-mcp-proxy"},
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        route_result = route.route_task(events[0])

        self.assertTrue(events[0]["has_open_dd_pr"])
        self.assertEqual(events[0]["workstream_id"], "github:example/backend!602")
        self.assertEqual(events[0]["origin_workstream_id"], "github:example/backend#2878")
        self.assertEqual(events[0]["existing_pr_head_branch"], "codex/issue-2878-mcp-proxy")
        self.assertEqual(events[0]["pr_author_login"], "robert-bot")
        self.assertEqual(route_result["expected_output"], "update_existing_pr")
        self.assertIn("push_existing_pr", route_result["allowed_github_actions"])
        self.assertNotIn("open_pr", route_result["allowed_github_actions"])

    def test_fixture_pr_body_extracts_dd_origin_workstream_without_live_lookup(self):
        from robert_agent import discover
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
        from robert_agent import discover
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
        from robert_agent import discover
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

        self.assertEqual(decisions[0]["authorization_status"], "accepted_context")
        self.assertTrue(decisions[0]["drives_execution"])

    def test_discover_cli_config_accepts_trusted_actors(self):
        from robert_agent import discover
        repo_config = discover.build_repo_config(
            "example/backend",
            "robert-bot",
            ["wklken"],
        )

        self.assertEqual(repo_config["trusted_actors"], ["wklken"])

    def test_notification_lookup_failure_stays_pending_authorization(self):
        from robert_agent import authorize
        from robert_agent import discover
        def fake_runner(args, **_kwargs):
            if "--assignee" in args or "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "n-failed-lookup",
                          "updated_at": "2026-06-16T00:01:00Z",
                          "repository": {
                            "full_name": "example/backend"
                          },
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/502"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/502/timeline"]:
                raise subprocess.CalledProcessError(1, args, stderr="network failed")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/502"]:
                return _Completed(
                    args,
                    """{
                      "number": 502,
                      "state": "open",
                      "title": "Lookup failure",
                      "updated_at": "2026-06-16T00:01:00Z",
                      "html_url": "https://github.com/example/backend/issues/502"
                    }""",
                )
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)
        decisions = authorize.authorize_events(events, REPO_CONFIG, existing_workstreams=set())

        self.assertEqual(events[0]["authorization_lookup_complete"], False)
        self.assertEqual(decisions[0]["authorization_status"], "pending_authorization")


class _Completed:
    def __init__(self, args, stdout):
        self.args = args
        self.stdout = stdout
        self.returncode = 0


if __name__ == "__main__":
    unittest.main()

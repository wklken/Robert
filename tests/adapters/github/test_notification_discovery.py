import subprocess
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


class NotificationDiscoveryTests(unittest.TestCase):
    def normalize_and_authorize(self, raw_events, existing_workstreams=None):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

        events = discover.normalize_events(raw_events, REPO_CONFIG)
        return authorize.authorize_events(
            events,
            REPO_CONFIG,
            known_workstreams=existing_workstreams or set(),
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

        self.assertEqual(
            decisions[0]["authorization_status"], "ignored_untrusted_trigger"
        )

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
        from robert_agent.adapters.github import discovery as discover

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

        buckets = discover.collect_account_notifications(
            [repo_a, repo_b], runner=fake_runner
        )

        self.assertEqual(sorted(buckets), ["Org/repo-a", "Org/repo-b"])
        self.assertEqual(buckets["Org/repo-a"][0]["repo_full_name"], "Org/repo-a")
        self.assertEqual(buckets["Org/repo-a"][0]["number"], 10)
        self.assertEqual(buckets["Org/repo-b"][0]["repo_full_name"], "Org/repo-b")
        self.assertEqual(buckets["Org/repo-b"][0]["source_type"], "pull_request")

    def test_repo_live_events_accept_preloaded_notification_hints_without_refetching_notifications(
        self,
    ):
        from robert_agent.adapters.github import discovery as discover

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
                      "body": "Suppress expected plugin 404 warning logs",
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
        self.assertEqual(events[0]["body"], "Suppress expected plugin 404 warning logs")
        self.assertNotIn(["gh", "api", "notifications", "--paginate", "--slurp"], calls)

    def test_repo_live_events_ignores_mismatched_preloaded_notification_hints(self):
        from robert_agent.adapters.github import discovery as discover

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
        self.assertFalse(
            any("repos/Org/repo-a/issues/10" in " ".join(args) for args in calls)
        )

    def test_closed_issue_notification_hint_is_ignored(self):
        from robert_agent.adapters.github import discovery as discover

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
                call[:3]
                == [
                    "gh",
                    "api",
                    "repos/example/backend/issues/2878/comments",
                ]
                for call in calls
            )
        )

    def test_notification_assignment_reuses_assignment_fingerprint(self):
        from robert_agent.adapters.github import discovery as discover

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
        self.assertEqual(
            {event["event_type"] for event in events}, {"assigned", "notification"}
        )

    def test_notification_for_known_pr_workstream_uses_latest_non_bot_comment(self):
        from robert_agent.adapters.github import discovery as discover

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

    def test_notification_lookup_failure_stays_pending_authorization(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

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
        decisions = authorize.authorize_events(
            events, REPO_CONFIG, existing_workstreams=set()
        )

        self.assertEqual(events[0]["authorization_lookup_complete"], False)
        self.assertEqual(decisions[0]["authorization_status"], "pending_authorization")

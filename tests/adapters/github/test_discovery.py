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


class DiscoveryTests(unittest.TestCase):
    def normalize_and_authorize(self, raw_events, existing_workstreams=None):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

        events = discover.normalize_events(raw_events, REPO_CONFIG)
        return authorize.authorize_events(
            events,
            REPO_CONFIG,
            known_workstreams=existing_workstreams or set(),
        )

    def test_live_unknown_association_is_enriched_with_repo_permission(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

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
        self.assertEqual(decisions[0]["authorization_status"], "context_only")

    def test_live_discovery_uses_gh_search_and_notification_hints(self):
        from robert_agent.adapters.github import discovery as discover

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

    def test_live_assignment_is_enriched_from_timeline_actor(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

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
                        "id": "assign-event-old",
                        "event": "assigned",
                        "created_at": "2026-06-16T00:01:00Z",
                        "actor": {"login": "wklken"},
                        "assignee": {"login": "robert-bot"}
                      },
                      {
                        "id": "assign-event-new",
                        "event": "assigned",
                        "created_at": "2026-06-16T00:02:00Z",
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
        self.assertEqual(events[0]["event_fingerprint"], "assigned:assign-event-new")
        self.assertEqual(decisions[0]["authorization_status"], "authorized_trigger")

    def test_live_assignment_without_timeline_actor_stays_pending(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

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
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

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
        from robert_agent.adapters.github import discovery as discover

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
                      "head": {
                        "ref": "codex/dd-123-fix-timeout",
                        "sha": "head-sha-123"
                      }
                    }""",
                )
            if tuple(args[:3]) in {
                ("gh", "api", "repos/example/backend/issues/601/comments"),
                ("gh", "api", "repos/example/backend/pulls/601/reviews"),
                ("gh", "api", "repos/example/backend/pulls/601/comments"),
            }:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            raise AssertionError(args)

        raw_events = discover.collect_live_events(REPO_CONFIG, runner=fake_runner)
        events = discover.normalize_events(raw_events, REPO_CONFIG)

        self.assertEqual(events[0]["workstream_id"], "github:example/backend!601")
        self.assertEqual(events[0]["origin_workstream_id"], "github:example/backend#123")
        self.assertTrue(events[0]["has_open_dd_pr"])
        self.assertEqual(events[0]["existing_pr_head_branch"], "codex/dd-123-fix-timeout")
        self.assertEqual(events[0]["existing_pr_head_sha"], "head-sha-123")
        self.assertEqual(events[0]["metadata"]["dd_workstream"]["task_id"], "task-parent")

    def test_live_pr_followup_collects_all_trusted_comments(self):
        from robert_agent.adapters.github import discovery as discover

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

        self.assertEqual(
            [event["event_fingerprint"] for event in events],
            ["comment:comment-old", "comment:comment-new"],
        )
        self.assertEqual(
            [event["body"] for event in events],
            [
                "@robert-bot old follow-up",
                "@robert-bot handle the latest review feedback",
            ],
        )
        self.assertEqual(
            {event["workstream_id"] for event in events},
            {"github:example/backend!603"},
        )
        self.assertEqual(
            {event["existing_pr_head_branch"] for event in events},
            {"codex/issue-2884-fix-operator"},
        )

    def test_live_known_pr_workstream_accepts_collaborator_followup(self):
        from robert_agent.security import authorization as authorize
        from robert_agent.adapters.github import discovery as discover

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
        self.assertEqual(decisions[0]["authorization_status"], "context_only")
        self.assertFalse(decisions[0]["drives_execution"])

    def test_live_robot_authored_pr_without_hidden_metadata_preserves_branch_for_classification(
        self,
    ):
        from robert_agent.adapters.github import discovery as discover
        from robert_agent.workflows import routes as route

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
            if tuple(args[:3]) in {
                ("gh", "api", "repos/example/backend/issues/602/comments"),
                ("gh", "api", "repos/example/backend/pulls/602/reviews"),
                ("gh", "api", "repos/example/backend/pulls/602/comments"),
            }:
                return _Completed(args, "[]")
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
        self.assertEqual(route_result["expected_output"], "classification_result")
        self.assertEqual(route_result["allowed_github_actions"], [])

    def test_discover_cli_config_accepts_trusted_actors(self):
        from robert_agent.adapters.github import discovery as discover

        repo_config = discover.build_repo_config(
            "example/backend",
            "robert-bot",
            ["wklken"],
        )

        self.assertEqual(repo_config["trusted_actors"], ["wklken"])

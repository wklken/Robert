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


class _Completed:
    def __init__(self, args, stdout):
        self.args = args
        self.stdout = stdout


def _empty_discovery(args):
    if "--assignee" in args or "--mentions" in args:
        return _Completed(args, "[]")
    raise AssertionError(args)


class GitHubEventMatrixTests(unittest.TestCase):
    def collect_normalized(self, fake_runner, known_workstreams=None):
        from robert_agent import discover
        raw_events = discover.collect_live_events(
            REPO_CONFIG,
            runner=fake_runner,
            known_workstreams=known_workstreams or set(),
        )
        return discover.normalize_events(raw_events, REPO_CONFIG)

    def collect_decisions(self, fake_runner, known_workstreams=None):
        from robert_agent import authorize
        events = self.collect_normalized(
            fake_runner,
            known_workstreams=known_workstreams,
        )
        return authorize.authorize_events(
            events,
            REPO_CONFIG,
            known_workstreams=known_workstreams or set(),
        )

    def test_notification_mention_resolves_to_trusted_comment_trigger(self):
        def fake_runner(args, **_kwargs):
            try:
                return _empty_discovery(args)
            except AssertionError:
                pass
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-mention",
                          "updated_at": "2026-06-22T12:10:00Z",
                          "reason": "mention",
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/3101"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3101"]:
                return _Completed(
                    args,
                    """{
                      "number": 3101,
                      "state": "open",
                      "title": "Needs analysis",
                      "updated_at": "2026-06-22T12:09:00Z",
                      "html_url": "https://github.com/example/backend/issues/3101"
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3101/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3101/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-3101",
                        "body": "@robert-bot please analyze this",
                        "created_at": "2026-06-22T12:10:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        decisions = self.collect_decisions(fake_runner)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["event_type"], "comment")
        self.assertEqual(decisions[0]["event_fingerprint"], "comment:comment-3101")
        self.assertEqual(decisions[0]["authorization_status"], "authorized_trigger")
        self.assertTrue(decisions[0]["creates_task"])

    def test_notification_without_actionable_source_remains_ignored(self):
        def fake_runner(args, **_kwargs):
            try:
                return _empty_discovery(args)
            except AssertionError:
                pass
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-noise",
                          "updated_at": "2026-06-22T12:20:00Z",
                          "reason": "subscribed",
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/3102"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3102"]:
                return _Completed(
                    args,
                    """{
                      "number": 3102,
                      "state": "open",
                      "title": "Watched issue",
                      "updated_at": "2026-06-22T12:20:00Z",
                      "html_url": "https://github.com/example/backend/issues/3102"
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3102/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3102/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-noise",
                        "body": "FYI, no bot mention here",
                        "created_at": "2026-06-22T12:19:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        decisions = self.collect_decisions(fake_runner)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["event_type"], "notification")
        self.assertEqual(decisions[0]["authorization_status"], "ignored_untrusted_trigger")
        self.assertFalse(decisions[0]["creates_task"])

    def test_notification_review_request_resolves_to_review_request_trigger(self):
        def fake_runner(args, **_kwargs):
            try:
                return _empty_discovery(args)
            except AssertionError:
                pass
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-review-request",
                          "updated_at": "2026-06-22T12:30:00Z",
                          "reason": "review_requested",
                          "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/example/backend/pulls/3103"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3103"]:
                return _Completed(
                    args,
                    """{
                      "number": 3103,
                      "state": "open",
                      "title": "Review this PR",
                      "updated_at": "2026-06-22T12:30:00Z",
                      "html_url": "https://github.com/example/backend/pull/3103"
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/3103"]:
                return _Completed(
                    args,
                    """{
                      "body": "Third-party PR",
                      "head": {"ref": "feature/pr-3103"},
                      "base": {"ref": "master"},
                      "user": {"login": "contributor"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3103/timeline"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "review-request-3103",
                        "event": "review_requested",
                        "created_at": "2026-06-22T12:30:00Z",
                        "actor": {"login": "wklken"},
                        "requested_reviewer": {"login": "robert-bot"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        decisions = self.collect_decisions(fake_runner)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["event_type"], "review_request")
        self.assertEqual(decisions[0]["event_fingerprint"], "review_request:review-request-3103")
        self.assertEqual(decisions[0]["requester_login"], "wklken")
        self.assertEqual(decisions[0]["requested_reviewer"], "robert-bot")
        self.assertEqual(decisions[0]["base_branch"], "master")
        self.assertEqual(decisions[0]["authorization_status"], "authorized_trigger")

    def test_known_pr_notification_uses_latest_non_bot_review_comment_context(self):
        known = {"github:example/backend!3104"}

        def fake_runner(args, **_kwargs):
            try:
                return _empty_discovery(args)
            except AssertionError:
                pass
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-pr-context",
                          "updated_at": "2026-06-22T12:40:00Z",
                          "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/example/backend/pulls/3104"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3104"]:
                return _Completed(
                    args,
                    """{
                      "number": 3104,
                      "state": "open",
                      "title": "DD PR follow-up",
                      "updated_at": "2026-06-22T12:40:00Z",
                      "html_url": "https://github.com/example/backend/pull/3104"
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/3104"]:
                return _Completed(
                    args,
                    """{
                      "body": "<!-- robert-workstream\\norigin_workstream_id: github:example/backend#3000\\nsource_issue: 3000\\ntask_id: task-parent\\ncreated_by: robert\\n-->",
                      "head": {"ref": "codex/dd-3000-fix"},
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3104/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3104/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-older",
                        "body": "older context",
                        "created_at": "2026-06-22T12:35:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/3104/reviews"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "review-middle",
                        "body": "middle review body",
                        "submitted_at": "2026-06-22T12:36:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"}
                      }
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/3104/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "review-comment-latest",
                        "body": "@robert-bot latest diff feedback",
                        "created_at": "2026-06-22T12:39:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"}
                      },
                      {
                        "id": "bot-review-comment",
                        "body": "bot echo",
                        "created_at": "2026-06-22T12:40:00Z",
                        "author_association": "MEMBER",
                        "user": {"login": "robert-bot"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        decisions = self.collect_decisions(fake_runner, known_workstreams=known)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["event_type"], "review_comment")
        self.assertEqual(decisions[0]["event_fingerprint"], "review_comment:review-comment-latest")
        self.assertEqual(decisions[0]["body"], "@robert-bot latest diff feedback")
        self.assertEqual(decisions[0]["authorization_status"], "accepted_context")
        self.assertTrue(decisions[0]["drives_execution"])

    def test_closed_notification_source_is_ignored_before_discussion_lookup(self):
        calls = []

        def fake_runner(args, **_kwargs):
            calls.append(args)
            try:
                return _empty_discovery(args)
            except AssertionError:
                pass
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-closed",
                          "updated_at": "2026-06-22T12:50:00Z",
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/3105"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/3105"]:
                return _Completed(
                    args,
                    """{
                      "number": 3105,
                      "state": "closed",
                      "state_reason": "completed",
                      "title": "Closed source",
                      "updated_at": "2026-06-22T12:50:00Z",
                      "closed_at": "2026-06-22T12:50:00Z",
                      "html_url": "https://github.com/example/backend/issues/3105"
                    }""",
                )
            raise AssertionError(args)

        events = self.collect_normalized(fake_runner)

        self.assertEqual(events, [])
        self.assertFalse(
            any(
                call[:3]
                == ["gh", "api", "repos/example/backend/issues/3105/comments"]
                for call in calls
            )
        )


if __name__ == "__main__":
    unittest.main()

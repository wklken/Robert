"""Review equivalence depends on authority, scope, plan and acceptance."""

from copy import deepcopy
import unittest

from robert_agent.work import review_identity


class ReviewIdentityTests(unittest.TestCase):
    def setUp(self):
        self.repo = {"full_name": "x/y", "github_account": "bot", "trusted_actors": ["maintainer"]}
        self.event = {
            "source_type": "pull_request",
            "number": 1,
            "workstream_id": "github:x/y!1",
            "source_key": "github:x/y!1",
            "event_fingerprint": "comment:one",
            "event_type": "comment",
            "actor_login": "maintainer",
            "pr_author_login": "author",
            "body": "@bot review this PR",
            "body_authorization_complete": True,
            "intent": "review_request",
        }
        self.workers = [{"name": "one", "model": "model-a"}, {"name": "two", "model": "model-b"}]
        self.route = {
            "id": "review-pr",
            "native_review_workers": self.workers,
            "verification_policy": {"mode": "optional"},
        }
        self.pr = {
            "state": "open",
            "number": 1,
            "user": {"login": "author"},
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40, "ref": "main"},
            "body": "Acceptance criteria",
            "title": "PR title",
        }

    def key(self, **changes):
        result = review_identity.canonical_request(
            changes.get("repo", self.repo),
            changes.get("event", self.event),
            changes.get("route", self.route),
            changes.get("workers", self.workers),
            changes.get("pr", self.pr),
        )
        return result["key"] if result else None

    def test_comment_and_native_request_share_key_across_authorization_and_workstream(self):
        request = self.event | {
            "event_type": "review_request",
            "event_fingerprint": "review_request:two",
            "body": "",
            "requested_reviewer": "bot",
            "requester_login": "maintainer",
            "workstream_id": "legacy",
        }
        self.assertIsNotNone(self.key())
        self.assertEqual(self.key(), self.key(event=request))

    def test_head_base_scope_plan_acceptance_and_eligibility_changes_do_not_merge(self):
        variants = []
        for field, value in (
            ("head", {"sha": "c" * 40}),
            ("base", {"sha": "d" * 40, "ref": "main"}),
            ("body", "Different acceptance"),
            ("title", "Different title"),
        ):
            variants.append({"pr": self.pr | {field: value}})
        variants.extend(
            [
                {"workers": list(reversed(self.workers))},
                {"route": self.route | {"verification_policy": {"mode": "required"}}},
                {"repo": self.repo | {"trusted_actors": ["maintainer", "other"]}},
                {"event": self.event | {"acceptance_intent": "independent"}},
                {"event": self.event | {"acceptance_criteria": ["extra"]}},
            ]
        )
        for change in variants:
            with self.subTest(change=change):
                self.assertNotEqual(self.key(), self.key(**change))

    def test_untrusted_or_incomplete_or_closed_observations_refused(self):
        for changes in (
            {"event": self.event | {"actor_login": "stranger"}},
            {"event": self.event | {"authorization_lookup_complete": False}},
            {"event": self.event | {"body_authorization_complete": False}},
            {"pr": self.pr | {"state": "closed"}},
            {"pr": self.pr | {"head": {"sha": "short"}}},
            {"pr": self.pr | {"user": {"login": "other"}}},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(self.key(**changes))

    def test_additional_material_and_nested_requests_remain_independent(self):
        for body in (
            "@bot review this PR again",
            "@bot review this PR\nFocus security",
            "@bot review this PR\n```\nrequirements\n```",
            "@bot review this PR\n> additional requirement",
        ):
            self.assertIsNone(self.key(event=self.event | {"body": body}))
        self.assertIsNone(self.key(event=self.event | {"review_scope": ["src/a.py"]}))
        self.assertIsNone(self.key(event=self.event | {"metadata": {"additional_materials": ["extra"]}}))
        request = self.event | {
            "event_type": "review_request",
            "requested_reviewer": "bot",
            "body": "extra request",
            "source_body": "extra request",
        }
        self.assertIsNone(self.key(event=request))
        self.assertIsNone(self.key(event=request | {"body": "", "intent": "independent_rereview"}))

    def test_identity_snapshots_input_values(self):
        result = review_identity.canonical_request(self.repo, self.event, self.route, self.workers, self.pr)
        identity = deepcopy(result["identity"])
        self.route["verification_policy"]["mode"] = "required"
        self.assertEqual(result["identity"], identity)

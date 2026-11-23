"""A request's human owner survives retries and derived PR discussion."""

import json
import sqlite3
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from tests.support.temp_workspace import TemporaryWorkspace
from robert_agent.persistence.schema import database
from robert_agent.work import decision_owner


class DecisionOwnerTests(unittest.TestCase):
    def event(self, **changes):
        return {
            "source_type": "issue",
            "source_key": "github:x/y#1",
            "event_fingerprint": "assigned:1",
            "event_type": "notification",
            "actor_login": "github",
            "assigned_to": "robert-bot",
            "assignment_actor_login": "Carlmac",
            "author_login": "issue-author",
            "authorization_lookup_complete": True,
            "authorization_status": "authorized_trigger",
            "drives_execution": True,
            **changes,
        }

    def test_assignment_owner_is_triggering_user_not_issue_author_or_robot(self):
        owner = decision_owner.from_event(self.event(), "robert-bot")
        self.assertEqual(owner["login"], "Carlmac")
        self.assertEqual(owner["source_key"], "github:x/y#1")
        self.assertEqual(owner["event_fingerprint"], "assigned:1")

    def test_comment_and_review_request_use_authenticated_triggering_user(self):
        for changes in (
            {"event_type": "comment", "actor_login": "Alice", "assigned_to": None},
            {"event_type": "review_request", "requester_login": "Bob", "assigned_to": None},
        ):
            with self.subTest(changes=changes):
                owner = decision_owner.from_event(self.event(**changes), "robert-bot")
                self.assertEqual(owner["login"], changes.get("requester_login") or changes["actor_login"])

    def test_unknown_untrusted_system_and_robot_identities_have_no_owner(self):
        for changes in (
            {"authorization_status": "context_only"},
            {"authorization_lookup_complete": False},
            {"actor_kind": "github_system"},
            {"assignment_actor_login": "Robert-Bot"},
            {"assignment_actor_login": "not a login"},
            {"assignment_actor_login": None, "author_login": None},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(decision_owner.from_event(self.event(**changes), "robert-bot"))


class PersistedDecisionOwnerTests(TemporaryWorkspace):
    def setUp(self):
        super().setUp()
        path = self.root / "owner.sqlite3"
        database.init_database(path)
        self.conn = sqlite3.connect(path)
        self.addCleanup(self.conn.close)
        self.conn.executemany(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES(?,?,'robert-bot','main','/repo','/repo/.worktrees')",
            [("repo", "x/y"), ("other", "x/z")],
        )

    def stream(self, name, origin=None, repo="repo", metadata=None):
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,origin_workstream_id,lifecycle,metadata_json,created_at,updated_at) "
            "VALUES(?,?,?,'completed',?,'2026-11-20','2026-11-20')",
            (name, repo, origin, json.dumps(metadata or {})),
        )

    def trigger(self, name, login, fingerprint, status="authorized_trigger", relationship="trigger"):
        source = "source-" + fingerprint
        self.conn.execute(
            "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number) VALUES(?,'repo',?,'issue',1)",
            (source, source),
        )
        payload = {
            "source_type": "issue",
            "source_key": "github:x/y#1",
            "event_type": "comment",
            "actor_login": login,
            "authorization_status": status,
            "drives_execution": True,
            "event_fingerprint": fingerprint,
        }
        self.conn.execute(
            "INSERT INTO github_events(event_id,repo_id,source_id,event_fingerprint,event_type,actor_login,"
            "authorization_status,event_at,payload_json) VALUES(?,'repo',?,?,'comment',?,?,?,?)",
            (fingerprint, source, fingerprint, login, status, fingerprint, json.dumps(payload)),
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at,metadata_json) "
            "VALUES(?,?,'completed',?,?,?)",
            (fingerprint, name, fingerprint, fingerprint, json.dumps({"request_event_fingerprint": fingerprint})),
        )
        self.conn.execute(
            "INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES(?,?,?,?)",
            (fingerprint, fingerprint, relationship, fingerprint),
        )
        return payload

    def test_owner_is_fixed_and_inherited_by_derived_pr_and_nested_followup(self):
        self.stream("issue", metadata={"unrelated": "preserved"})
        first = self.trigger("issue", "Alice", "001")
        decision_owner.record(self.conn, "issue", first, "robert-bot", "now")
        self.stream("pr", origin="issue")
        self.stream("backport", origin="pr")
        later = self.trigger("pr", "Bob", "002")
        decision_owner.record(self.conn, "pr", later, "robert-bot", "later")
        self.assertEqual(decision_owner.for_workstream(self.conn, "backport", "robert-bot")["login"], "Alice")
        metadata = json.loads(
            self.conn.execute("SELECT metadata_json FROM workstreams WHERE workstream_id='issue'").fetchone()[0]
        )
        self.assertEqual(metadata["unrelated"], "preserved")
        self.assertEqual(metadata["decision_owner"]["event_fingerprint"], "001")

    def test_legacy_request_uses_original_accepted_trigger_not_context_or_later_comment(self):
        self.stream("issue")
        self.trigger("issue", "Eve", "000", status="context_only", relationship="context")
        self.trigger("issue", "Alice", "001")
        self.trigger("issue", "Bob", "002")
        self.stream("pr", origin="issue")
        self.assertEqual(decision_owner.for_workstream(self.conn, "pr", "robert-bot")["login"], "Alice")

    def test_unknown_origin_cannot_be_taken_over_by_a_new_commenter(self):
        self.stream("issue")
        self.stream("pr", origin="issue")
        later = self.trigger("pr", "Bob", "002")
        self.assertIsNone(decision_owner.record(self.conn, "pr", later, "robert-bot", "later"))
        self.assertIsNone(decision_owner.for_workstream(self.conn, "pr", "robert-bot"))

    def test_owner_cannot_be_inherited_from_another_repository_or_a_cycle(self):
        owner = {"login": "Alice", "source_key": "github:x/z#1", "event_fingerprint": "001"}
        self.stream("other-issue", repo="other", metadata={"decision_owner": owner})
        self.stream("pr", origin="other-issue")
        self.assertIsNone(decision_owner.for_workstream(self.conn, "pr", "robert-bot"))
        self.stream("a", origin="b")
        self.stream("b", origin="a")
        self.assertIsNone(decision_owner.for_workstream(self.conn, "a", "robert-bot"))

    def test_discovered_robot_pr_inherits_owner_and_fixes_unknown_origin(self):
        self.stream("issue")
        first = self.trigger("issue", "Alice", "001")
        decision_owner.record(self.conn, "issue", first, "robert-bot", "now")
        self.stream("pr")
        followup = self.trigger("pr", "Bob", "002") | {"origin_workstream_id": "issue", "pr_author_login": "Robert-Bot"}
        self.assertEqual(decision_owner.record(self.conn, "pr", followup, "robert-bot", "now")["login"], "Alice")
        self.stream("missing-pr")
        unknown = followup | {"origin_workstream_id": "missing"}
        self.assertIsNone(decision_owner.record(self.conn, "missing-pr", unknown, "robert-bot", "now"))
        self.assertIsNone(
            decision_owner.record(
                self.conn, "missing-pr", followup | {"origin_workstream_id": None}, "robert-bot", "later"
            )
        )

    def test_initial_unidentified_owner_stays_unknown_after_a_later_human_request(self):
        self.stream("issue")
        first = self.trigger("issue", "github", "001")
        self.assertIsNone(decision_owner.record(self.conn, "issue", first, "robert-bot", "now"))
        later = self.trigger("issue", "Bob", "002")
        self.assertIsNone(decision_owner.record(self.conn, "issue", later, "robert-bot", "later"))

    def test_legacy_trigger_survives_cancellation_and_missing_request_metadata(self):
        self.stream("issue")
        self.trigger("issue", "Alice", "001")
        self.conn.execute("UPDATE tasks SET metadata_json='{}',lifecycle='canceled' WHERE task_id='001'")
        self.trigger("issue", "Bob", "002")
        self.assertEqual(decision_owner.for_workstream(self.conn, "issue", "robert-bot")["login"], "Alice")

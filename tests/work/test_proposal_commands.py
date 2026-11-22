from contextlib import closing
import json
import sqlite3

from tests.support.board_control_integration import BoardControlIntegrationFixture
from robert_agent.work.errors import WorkItemConflictError, WorkItemNotFoundError
from robert_agent.work.application import proposal_commands


class ProposalCommandsTests(BoardControlIntegrationFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO proposals (proposal_id,source_identity,repo_id,status,version,revision,source_key,"
                "fingerprint,created_at,updated_at) VALUES ('p-1','github:example/repo:11',?,'pending',1,1,"
                "'example/repo:issue:1','fingerprint','2026-11-22T01:00:00+00:00','2026-11-22T01:00:00+00:00')",
                (self.repo_id,),
            )
            conn.execute(
                "INSERT INTO proposal_revisions VALUES ('p-1',1,?,?,'2026-11-22T01:00:00+00:00')",
                (json.dumps({"provider": "github"}), json.dumps({"kind": "investigation"})),
            )

    def decide(self, action="approve", key="approve-1", version=1, context=None):
        return proposal_commands.decide(
            self.db_path,
            "p-1",
            context=context or self.context,
            action=action,
            expected_version=version,
            idempotency_key=key,
        )

    def test_approval_is_durable_and_replay_creates_one_decision(self):
        first = self.decide()
        self.assertEqual(first["status"], "approved")
        self.assertEqual(first, self.decide())
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_stale_approval_and_changed_idempotent_action_fail(self):
        self.decide()
        with self.assertRaises(WorkItemConflictError):
            self.decide(key="approve-2")
        with self.assertRaises(WorkItemConflictError):
            self.decide("hold")

    def test_hold_then_approve_and_discard_are_explicit(self):
        held = self.decide("hold")
        self.assertEqual(held["status"], "held")
        self.assertEqual(self.decide(key="approve-2", version=2)["status"], "approved")

    def test_repository_scope_is_checked_before_replay(self):
        context = type(self.context)("operator", "other", frozenset(), frozenset())
        self.decide()
        with self.assertRaises(WorkItemNotFoundError):
            self.decide(context=context)

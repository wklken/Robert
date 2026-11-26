"""An operator can defer and restore an item without granting execution authority."""

from contextlib import closing
from unittest.mock import patch

from robert_agent.persistence import database
from robert_agent.persistence.stores import proposals
from robert_agent.work.application.proposal_commands import decide
from tests.support.work_items import WorkItemCommandFixture


class ProposalDailyInboxTests(WorkItemCommandFixture):
    def test_deferral_expiry_and_manual_restore_are_versioned(self):
        with closing(database.connect(self.db_path)) as conn, conn:
            proposals.create(
                conn,
                {
                    "proposal_id": "p-1",
                    "source_identity": "github:issue:1",
                    "repo_id": "repo-1",
                    "status": "pending",
                    "version": 1,
                    "revision": 1,
                    "source_key": "github:example/repo#1",
                    "fingerprint": "test",
                    "created_at": "2026-11-25T00:00:00Z",
                    "updated_at": "2026-11-25T00:00:00Z",
                },
            )
            proposals.record_revision(
                conn,
                {
                    "proposal_id": "p-1",
                    "revision": 1,
                    "source_json": "{}",
                    "report_json": "{}",
                    "created_at": "2026-11-25T00:00:00Z",
                },
            )
        with patch(
            "robert_agent.work.application.proposal_commands.values.utc_now", return_value="2026-11-25T01:00:00Z"
        ):
            held = decide(
                self.db_path,
                "p-1",
                context=self.context,
                action="hold",
                expected_version=1,
                idempotency_key="hold-2-days",
                defer_days=2,
            )
            self.assertEqual(held["status"], "held")
            self.assertEqual(held["defer_until"], "2026-11-26T16:00:00+00:00")
            discarded = decide(
                self.db_path,
                "p-1",
                context=self.context,
                action="discard",
                expected_version=2,
                idempotency_key="discard-postponed",
            )
            self.assertEqual(discarded["status"], "discarded")
            restored = decide(
                self.db_path,
                "p-1",
                context=self.context,
                action="restore",
                expected_version=3,
                idempotency_key="restore-discarded",
            )
            self.assertEqual((restored["status"], restored["defer_until"]), ("pending", None))

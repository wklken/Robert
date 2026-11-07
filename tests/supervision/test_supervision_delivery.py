from robert_agent.delivery.application import receipts as publication_receipts
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import execution_control as execution_store
from robert_agent.acceptance import publish_dedupe as publish_dedupe_acceptance
from robert_agent.adapters.sqlite import database as storage


class DeliveryReceiptTests(unittest.TestCase):
    def test_confirmation_preserves_remote_evidence_and_binds_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite3"
            storage.init_database(db)
            publish_dedupe_acceptance._insert_fixture(db)
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                action = dict(
                    conn.execute(
                        "SELECT * FROM github_actions WHERE action_id='action-comment'"
                    ).fetchone()
                )
                intent = execution_store.prepare_publication_intent(
                    conn,
                    action_id=action["action_id"],
                    idempotency_key="receipt-test",
                    request_fingerprint="fingerprint",
                    now="2026-11-06T10:00:00+00:00",
                )
                intent = execution_store.transition_publication_intent(
                    conn,
                    intent["publication_id"],
                    "executing",
                    "2026-11-06T10:00:00+00:00",
                    remote_evidence={"source_sha": "a" * 40},
                )
                evidence = {
                    "external_id": "987",
                    "target_url": "https://github.com/x/y/issues/1#issuecomment-987",
                    "marker": "verified-comment-marker",
                }
                publication_receipts.mark_published(
                    conn,
                    action,
                    {
                        "deduplicated": True,
                        "external_id": "987",
                        "response": {"reconciliation": evidence},
                    },
                    "2026-11-06T10:01:00+00:00",
                    intent=intent,
                )
                persisted = json.loads(
                    conn.execute(
                        "SELECT remote_evidence_json FROM publication_intents"
                    ).fetchone()[0]
                )
                self.assertEqual(persisted["source_sha"], "a" * 40)
                self.assertEqual(persisted["reconciliation"], evidence)
                receipt = json.loads(
                    conn.execute(
                        "SELECT metadata_json FROM github_actions WHERE action_id='action-comment'"
                    ).fetchone()[0]
                )["publish"]["receipt"]
                self.assertEqual(receipt["action_id"], action["action_id"])
                self.assertEqual(receipt["result_id"], action["result_id"])
                self.assertEqual(
                    receipt["confirmation_method"], "remote_reconciliation"
                )
                self.assertEqual(receipt["external_id"], "987")
                self.assertEqual(persisted["receipt"], receipt)

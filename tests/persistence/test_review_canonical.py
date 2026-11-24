"""Atomic canonical admission retains original ownership and trigger identity."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from robert_agent.persistence.rounds import review
from robert_agent.persistence.schema.database import init_database


class CanonicalReviewStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "state.db"
        init_database(self.db)
        self.repo = {"full_name": "x/y"}
        self.route = {"id": "review-pr"}
        self.workers = [{"name": "one"}, {"name": "two"}]
        self.identity = {"key": "canonical-key", "identity": {"head_sha": "a" * 40, "base_sha": "b" * 40}}

    def event(self, fingerprint, stream="github:x/y!1"):
        return {
            "number": 1,
            "workstream_id": stream,
            "event_fingerprint": fingerprint,
            "authorization_status": "authorized_trigger",
            "drives_execution": True,
        }

    def test_concurrent_scanners_create_one_execution_with_two_durable_triggers(self):
        ready = Barrier(2)

        def enqueue(fingerprint):
            with review.connect(self.db) as conn:
                ready.wait(timeout=5)
                return review.enqueue(
                    conn, self.repo, self.event(fingerprint), self.route, self.workers, canonical_identity=self.identity
                )["round_id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            rounds = list(pool.map(enqueue, ["comment:one", "review_request:two"]))
        self.assertEqual(len(set(rounds)), 1)
        with review.connect(self.db) as conn:
            self.assertEqual(len(review.trigger_associations(conn, rounds[0])), 2)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM review_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM review_executions").fetchone()[0], 1)

    def test_same_pr_different_trigger_workstreams_share_round_but_preserve_source(self):
        with review.connect(self.db) as conn:
            first = review.enqueue(
                conn, self.repo, self.event("comment:one"), self.route, self.workers, canonical_identity=self.identity
            )
            second = review.enqueue(
                conn,
                self.repo,
                self.event("review_request:two", "legacy-stream"),
                self.route,
                self.workers,
                canonical_identity=self.identity,
            )
            self.assertEqual(first["round_id"], second["round_id"])
            self.assertEqual(
                {item["workstream_id"] for item in review.trigger_associations(conn, first["round_id"])},
                {"github:x/y!1", "legacy-stream"},
            )

    def test_fingerprint_replay_keeps_original_execution_even_after_changed_observation(self):
        with review.connect(self.db) as conn:
            first = review.enqueue(
                conn, self.repo, self.event("comment:one"), self.route, self.workers, canonical_identity=self.identity
            )
            second = review.enqueue(
                conn,
                self.repo,
                self.event("comment:one"),
                self.route,
                self.workers,
                canonical_identity={"key": "other-key", "identity": {"head_sha": "changed"}},
            )
            self.assertEqual(first, second)
            self.assertEqual(len(review.trigger_associations(conn, first["round_id"])), 1)

    def test_rollback_does_not_silently_commit_and_historical_round_is_untouched(self):
        with review.connect(self.db) as conn:
            old = review.enqueue(conn, self.repo, self.event("comment:historical"), self.route, self.workers)
            review.update(conn, old["round_id"], status="reviewing")
        with review.connect(self.db) as conn:
            new = review.enqueue(
                conn, self.repo, self.event("comment:new"), self.route, self.workers, canonical_identity=self.identity
            )
            self.assertTrue(conn.in_transaction)
            self.assertNotEqual(old["round_id"], new["round_id"])
            conn.rollback()
        with review.connect(self.db) as conn:
            self.assertIsNone(review.session(conn, round_id=new["round_id"]))
            self.assertEqual(review.session(conn, round_id=old["round_id"])["status"], "reviewing")

    def test_schema_upgrade_does_not_change_or_coalesce_historical_rows(self):
        with review.connect(self.db) as conn:
            first = review.enqueue(conn, self.repo, self.event("comment:old"), self.route, self.workers)
            second = review.enqueue(conn, self.repo, self.event("review_request:old"), self.route, self.workers)
        with sqlite3.connect(self.db) as conn:
            before = conn.execute("SELECT * FROM review_sessions").fetchall()
            conn.execute("DROP TABLE review_triggers")
            conn.execute("DROP TABLE review_executions")
        init_database(self.db)
        init_database(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT * FROM review_sessions").fetchall(), before)
            self.assertEqual(conn.execute("SELECT count(*) FROM review_executions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM review_triggers").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertNotEqual(first["round_id"], second["round_id"])

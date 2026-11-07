from tests.support import database as fixture_database
import unittest
from robert_agent.persistence.rounds import vote as vote_store
from robert_agent.persistence.rounds import pool as model_pool
from robert_agent.persistence.rounds import review as review_store
from tests.support import vote_coordinator as fixture_module


class VotePoolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.VoteCoordinatorFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.driver = self.fixture.start()
        self.driver.prepare()

    def test_review_and_vote_reservations_share_one_atomic_limit(self):
        with fixture_database.connect(self.fixture.db) as conn:
            review_store.configure_process_pool(conn, 1)
            review_store.enqueue(conn, self.driver.request["repo"], self.driver.request["event"], {}, [])
            review = conn.execute("SELECT round_id FROM review_sessions").fetchone()[0]
            r = review_store.new_run(
                conn, review, "review", "codex", self.driver.attempt_id, self.fixture.root / "review", "hash"
            )
            v = vote_store.new_run(
                conn,
                self.driver.round_id,
                "vote",
                "codex",
                self.driver.attempt_id,
                self.driver.manifest["artifact_root"],
                "hash",
            )
            for store, run in [(review_store, r), (vote_store, v)]:
                store.update_run(conn, run["run_id"], status="waiting")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self.assertTrue(review_store.reserve_slot(conn, r["run_id"]))
            self.assertFalse(vote_store.reserve_slot(conn, v["run_id"]))
            self.assertEqual(model_pool.status(conn), {"limit": 1, "in_use": 1})
            review_store.release_slot(conn, r["run_id"])
            self.assertTrue(vote_store.reserve_slot(conn, v["run_id"]))
            self.assertEqual(review_store.process_pool_status(conn), {"limit": 1, "in_use": 1})
            self.assertEqual(vote_store.process_pool_status(conn), {"limit": 1, "in_use": 1})

    def test_unknown_vote_launch_retains_slot_and_blocks_restart(self):
        with fixture_database.connect(self.fixture.db) as conn:
            run = vote_store.new_run(
                conn,
                self.driver.round_id,
                "vote",
                "codex",
                self.driver.attempt_id,
                self.driver.manifest["artifact_root"],
                "hash",
            )
            vote_store.update_run(conn, run["run_id"], status="starting", slot_reserved=1)
        from robert_agent.execution.stages import StageStopped

        with self.assertRaises(StageStopped):
            self.fixture.driver().run()
        with fixture_database.connect(self.fixture.db) as conn:
            self.assertEqual(model_pool.status(conn)["in_use"], 1)
            self.assertEqual(vote_store.session(conn, task_id=self.driver.task_id)["status"], "attention")

    def test_vote_reclaims_a_confirmed_dead_review_group_but_not_an_unknown_start(self):
        with fixture_database.connect(self.fixture.db) as conn:
            review_store.configure_process_pool(conn, 1)
            entry = review_store.enqueue(conn, self.driver.request["repo"], self.driver.request["event"], {}, [])
            run = review_store.new_run(
                conn,
                entry["round_id"],
                "review",
                "codex",
                self.driver.attempt_id,
                self.fixture.root / "review-orphan",
                "hash",
            )
            import json

            review_store.update_run(
                conn,
                run["run_id"],
                status="running",
                slot_reserved=1,
                dispatch_json=json.dumps({"pid": 1073741824, "process_group_id": 1073741824}),
            )
        self.driver.reclaim_finished_slots()
        with fixture_database.connect(self.fixture.db) as conn:
            self.assertEqual(model_pool.status(conn)["in_use"], 0)
            review_store.update_run(conn, run["run_id"], status="starting", slot_reserved=1, dispatch_json="{}")
        self.driver.reclaim_finished_slots()
        with fixture_database.connect(self.fixture.db) as conn:
            self.assertEqual(model_pool.status(conn)["in_use"], 1)

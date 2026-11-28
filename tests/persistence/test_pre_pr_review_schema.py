"""Stage upgrades preserve live reservations and exact legacy publication proof."""

import json
import sqlite3
import unittest

from robert_agent.persistence.schema import pre_pr_review


class PrePrReviewSchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY)")
        self.conn.execute("CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY)")
        self.conn.execute(
            "CREATE TABLE worker_results(result_id TEXT PRIMARY KEY, task_id TEXT, attempt_id TEXT, metadata_json TEXT)"
        )
        self.conn.execute("INSERT INTO tasks VALUES ('task')")
        self.conn.execute("INSERT INTO attempts VALUES ('attempt')")
        pre_pr_review.initialize(self.conn)
        self.proof = {
            "candidate_sha": "a" * 40,
            "head_sha": "b" * 40,
            "attempt_id": "attempt",
            "fingerprint": "fingerprint",
        }
        self.conn.execute(
            "INSERT INTO worker_results VALUES ('result','task','attempt',?)",
            (json.dumps({"submission_fingerprint": "fingerprint", "submission_head_sha": "b" * 40}),),
        )
        self.conn.execute(
            "INSERT INTO pre_pr_review_sessions(round_id,task_id,status,request_json,proof_json,deadline_at,"
            "final_result_id,created_at,updated_at) VALUES ('round','task','ready',?,?,?, 'result','created','updated')",
            (json.dumps({"original_request": "unchanged"}), json.dumps(self.proof), "2026-11-24T00:00:00+00:00"),
        )
        self.conn.execute(
            "INSERT INTO pre_pr_review_snapshots VALUES ('snapshot','round','head','base',?, 'created')",
            (json.dumps({"candidate": "sealed"}),),
        )

    def old_stage_contract(self, *, has_final_verifier, has_handoff_stages=False):
        ddl = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='pre_pr_review_runs'").fetchone()[0]
        ddl = ddl.replace(",'validate_deployment'", "")
        if not has_handoff_stages:
            ddl = ddl.replace(",'repair_handoff'", "").replace(",'validate_candidate'", "")
        if not has_final_verifier:
            ddl = ddl.replace(",'verify_final'", "")
        self.conn.execute("DROP TABLE pre_pr_review_runs")
        self.conn.execute(ddl)
        self.conn.execute(
            "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,"
            "output_dir,prompt_sha256,slot_reserved,dispatch_json,artifact_json,created_at,updated_at) "
            "VALUES ('live','round','review','reviewer',2,'attempt','running','sealed/run','prompt',1,?,?, 'created','updated')",
            (json.dumps({"pid": 1073741824}), json.dumps({"evidence": "sealed"})),
        )
        self.conn.commit()

    def rows(self, table):
        return self.conn.execute(f"SELECT * FROM {table}").fetchall()

    def test_handoff_stage_upgrade_preserves_live_rows_and_does_not_grandfather_newer_proof(self):
        self.old_stage_contract(has_final_verifier=True)
        before = {
            table: self.rows(table)
            for table in (
                "pre_pr_review_sessions",
                "pre_pr_review_snapshots",
                "pre_pr_review_runs",
                "worker_results",
            )
        }
        for _ in range(2):
            self.conn.execute("BEGIN IMMEDIATE")
            pre_pr_review.initialize(self.conn)
            self.assertTrue(self.conn.in_transaction)
            self.conn.commit()
            for table, rows in before.items():
                self.assertEqual(self.rows(table), rows, table)
            self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        ddl = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='pre_pr_review_runs'").fetchone()[0]
        self.assertIn("verify_final", ddl)
        self.assertIn("repair_handoff", ddl)
        self.assertIn("validate_candidate", ddl)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pre_pr_review_runs_reserved'").fetchone()
        )
        for stage in ("verify_final", "repair_handoff", "validate_candidate", "validate_deployment"):
            self.conn.execute(
                "INSERT INTO pre_pr_review_runs SELECT ?,round_id,?,worker_id,model,attempt_no,owner_attempt_id,status,"
                "?,prompt_sha256,slot_reserved,started_at,dispatch_json,artifact_json,error,created_at,updated_at "
                "FROM pre_pr_review_runs WHERE run_id='live'",
                (stage, stage, "output/" + stage),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE pre_pr_review_runs SET stage='unsupported' WHERE run_id='live'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE pre_pr_review_runs SET stage='review' WHERE run_id='verify_final'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE pre_pr_review_runs SET output_dir='sealed/run' WHERE run_id='verify_final'")

    def test_deployment_stage_upgrade_from_current_schema_preserves_live_rows(self):
        self.old_stage_contract(has_final_verifier=True, has_handoff_stages=True)
        session_rows = self.rows("pre_pr_review_sessions")
        run_rows = self.rows("pre_pr_review_runs")
        pre_pr_review.initialize(self.conn)
        self.assertEqual(self.rows("pre_pr_review_sessions"), session_rows)
        self.assertEqual(self.rows("pre_pr_review_runs"), run_rows)
        self.conn.execute("UPDATE pre_pr_review_runs SET stage='validate_deployment' WHERE run_id='live'")

    def test_oldest_upgrade_binds_only_matching_preexisting_result_and_preserves_evidence(self):
        self.old_stage_contract(has_final_verifier=False)
        before = {
            table: self.rows(table)
            for table in (
                "pre_pr_review_snapshots",
                "pre_pr_review_runs",
                "worker_results",
            )
        }
        pre_pr_review.initialize(self.conn)
        self.conn.commit()
        request, proof, deadline = self.conn.execute(
            "SELECT request_json,proof_json,deadline_at FROM pre_pr_review_sessions"
        ).fetchone()
        binding = json.loads(request)["legacy_review_binding"]
        self.assertEqual(
            (binding["result_id"], binding["attempt_id"], binding["fingerprint"]), ("result", "attempt", "fingerprint")
        )
        self.assertEqual(json.loads(request)["original_request"], "unchanged")
        self.assertEqual(json.loads(proof), self.proof)
        self.assertEqual(deadline, "2026-11-24T00:00:00+00:00")
        pre_pr_review.initialize(self.conn)
        self.assertEqual(self.conn.execute("SELECT request_json FROM pre_pr_review_sessions").fetchone()[0], request)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)

    def test_oldest_upgrade_rejects_mismatched_result_fingerprint(self):
        self.old_stage_contract(has_final_verifier=False)
        self.conn.execute(
            "UPDATE worker_results SET metadata_json=?",
            (
                json.dumps(
                    {
                        "submission_fingerprint": "different",
                        "submission_head_sha": "b" * 40,
                    }
                ),
            ),
        )
        pre_pr_review.initialize(self.conn)
        request = self.conn.execute("SELECT request_json FROM pre_pr_review_sessions").fetchone()[0]
        self.assertNotIn("legacy_review_binding", json.loads(request))

from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class RuntimeKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "dd.sqlite3"
        self.repo_id = "repo:example/backend"
        self.now = datetime.now(timezone.utc).isoformat()
        self._init_db()

    def test_propose_candidates_from_project_memory_without_activating_them(self):
        from robert_agent import project_memory, runtime_knowledge

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                self._result_payload(),
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            proposed = runtime_knowledge.propose_candidates(conn, self.repo_id, self.now)
            candidate = conn.execute(
                """
                SELECT status, title, candidate_type, source_memory_ids_json
                FROM knowledge_candidates
                """
            ).fetchone()
            runtime_count = conn.execute(
                "SELECT COUNT(*) FROM runtime_knowledge"
            ).fetchone()[0]

        self.assertEqual(proposed["status"], "proposed")
        self.assertEqual(proposed["candidate_count"], 1)
        self.assertEqual(candidate[0], "pending")
        self.assertEqual(candidate[1], "DD PR follow-up uses update-existing-pr")
        self.assertEqual(candidate[2], "rule")
        self.assertEqual(runtime_count, 0)

    def test_approve_candidate_creates_active_runtime_knowledge(self):
        from robert_agent import project_memory, runtime_knowledge

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                self._result_payload(),
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            runtime_knowledge.propose_candidates(conn, self.repo_id, self.now)
            candidate_id = conn.execute(
                "SELECT candidate_id FROM knowledge_candidates"
            ).fetchone()[0]
            approved = runtime_knowledge.approve_candidate(
                conn,
                candidate_id=candidate_id,
                scope_type="route",
                scope_value="update-existing-pr",
                approved_by="wklken",
                run_now=self.now,
            )
            loaded = runtime_knowledge.load_runtime_knowledge(
                conn,
                repo_id=self.repo_id,
                route_result={
                    "route_id": "update-existing-pr",
                    "expected_output": "update_existing_pr",
                },
                events=[],
            )
            candidate_status = conn.execute(
                "SELECT status, reviewer FROM knowledge_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(candidate_status, ("approved", "wklken"))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["title"], "DD PR follow-up uses update-existing-pr")
        self.assertIn("update the PR branch", loaded[0]["prompt_text"])

    def test_reject_candidate_keeps_runtime_knowledge_empty(self):
        from robert_agent import project_memory, runtime_knowledge

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                self._result_payload(),
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            runtime_knowledge.propose_candidates(conn, self.repo_id, self.now)
            candidate_id = conn.execute(
                "SELECT candidate_id FROM knowledge_candidates"
            ).fetchone()[0]
            rejected = runtime_knowledge.reject_candidate(
                conn,
                candidate_id=candidate_id,
                reviewer="wklken",
                review_note="too narrow",
                run_now=self.now,
            )
            loaded = runtime_knowledge.load_runtime_knowledge(
                conn,
                repo_id=self.repo_id,
                route_result={
                    "route_id": "update-existing-pr",
                    "expected_output": "update_existing_pr",
                },
                events=[],
            )

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(loaded, [])

    def test_memory_curator_cli_approve_prints_json(self):
        from robert_agent import project_memory, runtime_knowledge
        from robert_agent import memory_curator
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                self._result_payload(),
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            runtime_knowledge.propose_candidates(conn, self.repo_id, self.now)
            candidate_id = conn.execute(
                "SELECT candidate_id FROM knowledge_candidates"
            ).fetchone()[0]

        output = memory_curator.run_command(
            [
                "--db",
                str(self.db_path),
                "approve",
                "--candidate-id",
                candidate_id,
                "--scope-type",
                "route",
                "--scope-value",
                "update-existing-pr",
                "--approved-by",
                "wklken",
            ]
        )

        self.assertTrue(output["ok"], output)
        self.assertEqual(output["status"], "approved")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            runtime_count = conn.execute(
                "SELECT COUNT(*) FROM runtime_knowledge"
            ).fetchone()[0]
        self.assertEqual(runtime_count, 1)

    def _init_db(self):
        from robert_agent import storage

        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES (?, 'example/backend', 'robert-bot', 'master', '/repo', '/repo/.worktrees')
                """,
                (self.repo_id,),
            )

    def _result_payload(self):
        return {
            "result_id": "result-1",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "update_existing_pr",
            "memory_delta": {
                "status": "has_memory",
                "entries": [
                    {
                        "operation": "upsert",
                        "kind": "decision",
                        "title": "DD PR follow-up uses update-existing-pr",
                        "short_summary": "When review comments arrive on a DD PR, update the PR branch.",
                        "long_summary": "A DD PR has its own workstream; reviewer follow-up should not reopen the origin issue task.",
                        "paths": ["src/robert_agent/run_once.py"],
                        "symbols": ["_is_dd_pr_followup_event"],
                        "keywords": ["dd-pr-followup", "update-existing-pr"],
                        "confidence": "medium",
                        "evidence": [{"type": "test", "value": "python -B -m unittest"}],
                    }
                ],
            },
        }


if __name__ == "__main__":
    unittest.main()

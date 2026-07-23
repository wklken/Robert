from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "dd.sqlite3"
        self.repo_id = "repo:example/backend"
        self.now = datetime.now(timezone.utc).isoformat()
        self._init_db()

    def test_record_memory_delta_upserts_entry_and_revision(self):
        from robert_agent import project_memory

        result_payload = self._result_payload(
            title="PR review comments update the PR workstream",
            short_summary="DD PR follow-up runs on the PR workstream.",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            status = project_memory.record_memory_delta(
                conn,
                result_payload,
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            entry = conn.execute(
                """
                SELECT memory_thread_key, kind, title, short_summary, revision_count
                FROM project_memory_entries
                """
            ).fetchone()
            revision_count = conn.execute(
                "SELECT COUNT(*) FROM project_memory_revisions"
            ).fetchone()[0]
            terms = {
                row[0]
                for row in conn.execute(
                    "SELECT term_value FROM project_memory_terms WHERE term_type = 'keyword'"
                )
            }

        self.assertEqual(status["status"], "recorded")
        self.assertEqual(status["recorded_count"], 1)
        self.assertEqual(
            entry,
            (
                "github:example/backend!456",
                "decision",
                "PR review comments update the PR workstream",
                "DD PR follow-up runs on the PR workstream.",
                1,
            ),
        )
        self.assertEqual(revision_count, 1)
        self.assertIn("dd-pr-followup", terms)

    def test_record_memory_delta_appends_revision_for_same_thread_entry(self):
        from robert_agent import project_memory

        first = self._result_payload(
            result_id="result-1",
            title="PR review comments update the PR workstream",
            short_summary="First summary.",
        )
        second = self._result_payload(
            result_id="result-2",
            title="PR review comments update the PR workstream",
            short_summary="Updated summary after review.",
        )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                first,
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            project_memory.record_memory_delta(
                conn,
                second,
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            entry_count = conn.execute(
                "SELECT COUNT(*) FROM project_memory_entries"
            ).fetchone()[0]
            entry = conn.execute(
                "SELECT short_summary, revision_count FROM project_memory_entries"
            ).fetchone()
            revision_count = conn.execute(
                "SELECT COUNT(*) FROM project_memory_revisions"
            ).fetchone()[0]

        self.assertEqual(entry_count, 1)
        self.assertEqual(entry, ("Updated summary after review.", 2))
        self.assertEqual(revision_count, 2)

    def test_invalid_memory_delta_is_skipped_without_exception(self):
        from robert_agent import project_memory

        payload = {
            "result_id": "result-1",
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "memory_delta": {"status": "has_memory", "entries": "not-a-list"},
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            status = project_memory.record_memory_delta(
                conn,
                payload,
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            entry_count = conn.execute(
                "SELECT COUNT(*) FROM project_memory_entries"
            ).fetchone()[0]

        self.assertEqual(status["status"], "skipped")
        self.assertIn("entries", status["safe_error"])
        self.assertEqual(entry_count, 0)

    def test_retrieve_memories_ranks_keyword_and_route_matches(self):
        from robert_agent import project_memory

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                self._result_payload(
                    title="PR review comments update the PR workstream",
                    short_summary="DD PR follow-up runs on update-existing-pr.",
                    keywords=["dd-pr-followup", "review", "update-existing-pr"],
                ),
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            project_memory.record_memory_delta(
                conn,
                self._result_payload(
                    result_id="result-2",
                    title="Daily report notes use compact sections",
                    short_summary="Unrelated report memory.",
                    keywords=["70w", "report"],
                ),
                workstream_id="github:example/backend#999",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            memories = project_memory.retrieve_memories(
                conn,
                repo_id=self.repo_id,
                workstream_id="github:example/backend!456",
                route_result={
                    "route_id": "update-existing-pr",
                    "expected_output": "update_existing_pr",
                },
                events=[
                    {
                        "title": "Review follow-up",
                        "body": "@robert-bot handle this dd-pr-followup review",
                        "intent": "bug_fix",
                    }
                ],
            )

        self.assertGreaterEqual(len(memories), 1)
        self.assertEqual(memories[0]["title"], "PR review comments update the PR workstream")
        self.assertIn("memory_id", memories[0])
        self.assertEqual(memories[0]["short_summary"], "DD PR follow-up runs on update-existing-pr.")

    def test_retrieve_memories_applies_runtime_knowledge_boost_terms(self):
        from robert_agent import project_memory

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            project_memory.record_memory_delta(
                conn,
                self._result_payload(
                    title="Use update-existing-pr for DD PR review loops",
                    short_summary="DD PR follow-up should stay on the PR branch.",
                    keywords=["dd-pr-followup", "update-existing-pr"],
                ),
                workstream_id="github:example/backend!456",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            project_memory.record_memory_delta(
                conn,
                self._result_payload(
                    result_id="result-2",
                    title="General issue analysis",
                    short_summary="Issue analysis should inspect the reported module.",
                    keywords=["analysis", "issue"],
                ),
                workstream_id="github:example/backend#999",
                repo_id=self.repo_id,
                run_now=self.now,
            )
            memories = project_memory.retrieve_memories(
                conn,
                repo_id=self.repo_id,
                workstream_id="github:example/backend#124",
                route_result={
                    "route_id": "comment-analysis",
                    "expected_output": "comment_analysis",
                },
                events=[
                    {
                        "title": "Analyze this issue",
                        "body": "@robert-bot please analyze this issue",
                        "intent": "analysis",
                    }
                ],
                runtime_knowledge=[
                    {
                        "retrieval_boost": {
                            "keywords": ["dd-pr-followup", "update-existing-pr"]
                        }
                    }
                ],
            )

        self.assertGreaterEqual(len(memories), 1)
        self.assertEqual(memories[0]["title"], "Use update-existing-pr for DD PR review loops")

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

    def _result_payload(
        self,
        result_id="result-1",
        title="Memory title",
        short_summary="Short summary.",
        keywords=None,
    ):
        return {
            "result_id": result_id,
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "update_existing_pr",
            "memory_delta": {
                "status": "has_memory",
                "entries": [
                    {
                        "operation": "upsert",
                        "kind": "decision",
                        "title": title,
                        "short_summary": short_summary,
                        "long_summary": "Longer explanation for retrieval and prompt context.",
                        "paths": ["src/robert_agent/run_once.py"],
                        "symbols": ["_create_task_attempt_and_prompt"],
                        "keywords": list(keywords or ["dd-pr-followup", "workstream"]),
                        "confidence": "medium",
                        "evidence": [{"type": "test", "value": "python -B -m unittest"}],
                    }
                ],
            },
        }


if __name__ == "__main__":
    unittest.main()

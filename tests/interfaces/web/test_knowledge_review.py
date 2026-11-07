from contextlib import closing
import json
import sqlite3
from tests.support.dashboard import DashboardFixture


class KnowledgeReviewHTTPTests(DashboardFixture):
    def test_web_post_approves_knowledge_candidate_locally(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_knowledge_candidate(db_path)

        status, headers, body = web.handle_dashboard_post(
            "/knowledge/approve",
            (
                "candidate_id=kc-1&scope_type=route&scope_value=update-existing-pr"
                "&approved_by=wklken"
            ).encode("utf-8"),
            db_path,
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        result = json.loads(body.decode("utf-8"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "approved")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            candidate = conn.execute(
                "SELECT status, reviewer FROM knowledge_candidates WHERE candidate_id = 'kc-1'"
            ).fetchone()
            runtime_count = conn.execute(
                "SELECT COUNT(*) FROM runtime_knowledge WHERE candidate_id = 'kc-1'"
            ).fetchone()[0]
        self.assertEqual(candidate, ("approved", "wklken"))
        self.assertEqual(runtime_count, 1)

    def test_web_post_proposes_knowledge_candidates_locally(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_project_memory_entry(db_path)

        status, headers, body = web.handle_dashboard_post(
            "/knowledge/propose",
            "repo_id=repo-1".encode("utf-8"),
            db_path,
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        result = json.loads(body.decode("utf-8"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["candidate_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT title, status
                FROM knowledge_candidates
                WHERE repo_id = 'repo-1'
                """
            ).fetchone()
        self.assertEqual(row, ("Prefer update existing PR", "pending"))

    def test_web_post_rejects_knowledge_candidate_locally(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        self._record_knowledge_candidate(db_path)

        status, _headers, body = web.handle_dashboard_post(
            "/knowledge/reject",
            "candidate_id=kc-1&reviewer=wklken&review_note=too+narrow".encode("utf-8"),
            db_path,
        )

        self.assertEqual(status, 200)
        result = json.loads(body.decode("utf-8"))
        self.assertEqual(result["status"], "rejected")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            candidate = conn.execute(
                "SELECT status, reviewer, review_note FROM knowledge_candidates WHERE candidate_id = 'kc-1'"
            ).fetchone()
        self.assertEqual(candidate, ("rejected", "wklken", "too narrow"))

    def test_web_post_refuses_unknown_write_action(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, _headers, body = web.handle_dashboard_post(
            "/github/publish",
            b"action_id=action-1",
            db_path,
        )

        self.assertEqual(status, 404)
        self.assertIn(b"unsupported dashboard action", body)

    def test_web_post_refuses_knowledge_write_when_tables_are_missing(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "old.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("CREATE TABLE repos(repo_id TEXT PRIMARY KEY, full_name TEXT)")

        status, _headers, body = web.handle_dashboard_post(
            "/knowledge/propose",
            b"repo_id=repo-1",
            db_path,
        )

        self.assertEqual(status, 400)
        result = json.loads(body.decode("utf-8"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("Knowledge tables are not available", result["safe_error"])

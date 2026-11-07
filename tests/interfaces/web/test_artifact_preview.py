from contextlib import closing
import sqlite3
from tests.support.summary_database import SummaryFixture


class ArtifactPreviewHTTPTests(SummaryFixture):
    def test_web_serves_registered_text_artifact_preview(self):
        from robert_agent.interfaces.web import server as web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        log_path = self.root / "worker.stderr.log"
        log_path.write_text("line 1\nworker failed clearly\n", encoding="utf-8")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE artifacts
                SET path = ?, bytes = ?
                WHERE task_id = 'task-1' AND artifact_type = 'worker_stderr'
                """,
                (str(log_path), log_path.stat().st_size),
            )

        status, headers, body = web.build_http_response(
            "/artifact.txt?task_id=task-1&artifact_type=worker_stderr",
            db_path,
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn(b"worker failed clearly", body)
        self.assertIn(b"artifact_type=worker_stderr", body)

    def test_web_rejects_unregistered_artifact_preview(self):
        from robert_agent.interfaces.web import server as web
        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, headers, body = web.build_http_response(
            "/artifact.txt?task_id=task-2&artifact_type=worker_stderr",
            db_path,
        )

        self.assertEqual(status, 404)
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn(b"artifact not found", body)

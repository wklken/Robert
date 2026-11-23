"""HTTP and read-model regressions from the verified deep review."""

from contextlib import closing, redirect_stdout
import http.client
import io
import json
import sqlite3
import threading
from unittest.mock import patch

from robert_agent.interfaces.web import server as web
from robert_agent.readmodels import dashboard, execution_metrics
from tests.support.summary_database import SummaryFixture
from tests.support.board import BoardReadModelFixture


class WebBoundaryTests(SummaryFixture):
    def test_missing_database_is_reported_without_creating_a_file(self):
        missing = self.root / "missing.db"
        for extra in ([], ["--json"]):
            output = io.StringIO()
            with redirect_stdout(output), patch.object(web, "serve") as serve:
                code = web.main(["--db", str(missing), *extra])
            self.assertEqual(code, 3)
            self.assertFalse(json.loads(output.getvalue())["ok"])
            serve.assert_not_called()
            self.assertFalse(missing.exists())

    def test_config_read_only_database_is_not_created(self):
        missing = self.root / "config-missing.db"
        with (
            patch.object(web.validate_config, "validate_config", return_value={"ok": True, "db_path": str(missing)}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(web.main(["--config", "unused", "--json"]), 3)
        self.assertFalse(missing.exists())

    def test_default_http_port_origin_is_normalized(self):
        self.assertIn("http://127.0.0.1", web._allowed_web_origins("127.0.0.1", 80))

    def test_invalid_form_encoding_is_a_validation_error(self):
        status, _, body = web.handle_dashboard_post("/knowledge/propose", b"\xff", self.root / "unused.db")
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["status"], "failed_validation")

    def test_read_routes_reject_untrusted_hosts_and_disallow_framing(self):
        server = web.ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        context = web.build_read_only_control_context(self.root / "unused.db", host="127.0.0.1", port=port)
        server.RequestHandlerClass = web.make_handler(context.db_path, context)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        for host, expected in ((f"127.0.0.1:{port}", 200), ("attacker.example", 403)):
            with self.subTest(host=host):
                connection = http.client.HTTPConnection("127.0.0.1", port)
                self.addCleanup(connection.close)
                connection.request("GET", "/api/session", headers={"Host": host})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
                self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
                self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy") or "")

    def test_malformed_saved_metadata_does_not_break_dashboard_or_timing(self):
        db = self.root / "state.db"
        self._init_summary_db(db)
        for value in ("{bad", "[]", '"text"'):
            with self.subTest(value=value), closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("UPDATE worker_results SET metadata_json=?", (value,))
                conn.execute("UPDATE attempts SET metadata_json=?", (value,))
                conn.commit()
                self.assertIsNotNone(execution_metrics.task_timing(conn, "task-1"))
                self.assertIsInstance(dashboard.build_dashboard_data(db), dict)


class BoardBoundaryTests(BoardReadModelFixture):
    def test_attention_filter_rejects_unknown_boolean(self):
        status, _, body = web.build_http_response("/api/board?attention=perhaps", self.db_path)
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["status"], "failed_validation")

    def test_agent_options_do_not_depend_on_page_cursor(self):
        from robert_agent.readmodels import board

        self._seed_six_columns()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE work_items SET requested_worker=title, routing_mode='manual'")
        first = board.list_board(self.db_path, limit=1)
        page = first
        while page["next_cursor"]:
            page = board.list_board(self.db_path, limit=1, cursor=page["next_cursor"])
            self.assertEqual(first["filters"]["agents"], page["filters"]["agents"])

    def test_malformed_task_metadata_keeps_board_available(self):
        from robert_agent.readmodels import board

        self._create("test", start=True)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET metadata_json='{bad'")
        self.assertEqual(len(board.list_board(self.db_path)["items"]), 1)

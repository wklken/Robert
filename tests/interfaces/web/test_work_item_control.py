from contextlib import closing
import json
import sqlite3
from tests.support.summary_database import SummaryFixture
from tests.support.acceptance import AcceptanceFixture


class WorkItemControlHTTPTests(SummaryFixture, AcceptanceFixture):
    def test_web_retrigger_api_requires_control_auth_and_returns_child_task(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle = 'failed' WHERE task_id = 'task-2'")
            conn.execute(
                "UPDATE workstreams SET lifecycle = 'failed', active_task_id = NULL WHERE workstream_id = 'ws-1'"
            )
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset({"repo-1"}),
            allowed_workers=frozenset({"default"}),
            allowed_origins=frozenset({"http://127.0.0.1:8765"}),
            csrf_token="csrf-test",
            writes_enabled=True,
            write_error=None,
        )
        headers = {
            "host": "127.0.0.1:8765",
            "origin": "http://127.0.0.1:8765",
            "content-type": "application/json",
            "x-robert-csrf-token": "csrf-test",
            "x-idempotency-key": "retrigger-task-2",
        }

        denied_headers = dict(headers)
        denied_headers.pop("x-robert-csrf-token")
        denied_status, _response_headers, _body = web.handle_dashboard_post(
            "/api/tasks/task-2/retrigger",
            b"{}",
            db_path,
            control_context=context,
            request_headers=denied_headers,
        )
        status, _response_headers, body = web.handle_dashboard_post(
            "/api/tasks/task-2/retrigger",
            b"{}",
            db_path,
            control_context=context,
            request_headers=headers,
        )

        self.assertEqual(denied_status, 403)
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["task_id"], "task-2")
        self.assertEqual(payload["status"], "queued")
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM wakeups WHERE task_id = ?", (payload["task_id"],)).fetchone()[0],
                "pending",
            )

    def test_board_api_session_create_read_and_conflict_contract(self):
        from robert_agent.interfaces.web import server as web
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "board-api.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees')"
            )
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset({"repo-1"}),
            allowed_workers=frozenset({"default"}),
            allowed_origins=frozenset({"http://127.0.0.1:8765"}),
            csrf_token="csrf-test",
            writes_enabled=True,
            write_error=None,
        )
        session_status, _headers, session_body = web.build_http_response(
            "/api/session", db_path, control_context=context
        )
        self.assertEqual(session_status, 200)
        self.assertEqual(json.loads(session_body)["csrf_token"], "csrf-test")

        request_headers = {
            "host": "127.0.0.1:8765",
            "origin": "http://127.0.0.1:8765",
            "content-type": "application/json",
            "x-robert-csrf-token": "csrf-test",
            "x-idempotency-key": "create-api-1",
        }
        create_status, _headers, create_body = web.handle_dashboard_post(
            "/api/work-items",
            json.dumps(
                {
                    "repo_id": "repo-1",
                    "title": "API card",
                    "description": "Create from the local board.",
                    "priority": "P1",
                    "routing_mode": "auto",
                    "requested_worker": None,
                    "mode": "backlog",
                }
            ).encode(),
            db_path,
            control_context=context,
            request_headers=request_headers,
        )
        self.assertEqual(create_status, 201, create_body)
        created = json.loads(create_body)
        work_item_id = created["work_item_id"]

        board_status, _headers, board_body = web.build_http_response(
            "/api/board?repo=repo-1", db_path, control_context=context
        )
        detail_status, _headers, detail_body = web.build_http_response(
            f"/api/work-items/{work_item_id}", db_path, control_context=context
        )
        self.assertEqual(board_status, 200)
        self.assertEqual(detail_status, 200)
        self.assertEqual(json.loads(board_body)["items"][0]["title"], "API card")
        detail = json.loads(detail_body)

        missing_csrf = dict(request_headers)
        missing_csrf.pop("x-robert-csrf-token")
        denied_status, _headers, _body = web.handle_dashboard_post(
            f"/api/work-items/{work_item_id}/commands",
            json.dumps({"command": "start", "expected_version": detail["version"]}).encode(),
            db_path,
            control_context=context,
            request_headers=missing_csrf,
        )
        self.assertEqual(denied_status, 403)

        command_headers = {**request_headers, "x-idempotency-key": "start-api-1"}
        start_status, _headers, start_body = web.handle_dashboard_post(
            f"/api/work-items/{work_item_id}/commands",
            json.dumps({"command": "start", "expected_version": detail["version"]}).encode(),
            db_path,
            control_context=context,
            request_headers=command_headers,
        )
        self.assertEqual(start_status, 200, start_body)
        stale_headers = {**request_headers, "x-idempotency-key": "edit-stale-api"}
        stale_status, _headers, stale_body = web.handle_dashboard_post(
            f"/api/work-items/{work_item_id}/commands",
            json.dumps(
                {
                    "command": "edit",
                    "expected_version": detail["version"],
                    "title": "Stale edit",
                }
            ).encode(),
            db_path,
            control_context=context,
            request_headers=stale_headers,
        )
        self.assertEqual(stale_status, 409)
        self.assertEqual(json.loads(stale_body)["current"]["work_item_id"], work_item_id)

    def test_board_api_rejects_bad_filters_and_read_only_writes(self):
        from robert_agent.interfaces.web import server as web
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "board-readonly.sqlite3"
        storage.init_database(db_path)
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset(),
            allowed_workers=frozenset(),
            allowed_origins=frozenset({"http://127.0.0.1:8765"}),
            csrf_token="csrf-test",
            writes_enabled=False,
            write_error="Start with --config to enable writes.",
        )
        status, _headers, _body = web.build_http_response("/api/board?column=unknown", db_path, control_context=context)
        self.assertEqual(status, 422)
        write_status, _headers, _body = web.handle_dashboard_post(
            "/api/work-items",
            b"{}",
            db_path,
            control_context=context,
            request_headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
                "content-type": "application/json",
                "x-robert-csrf-token": "csrf-test",
                "x-idempotency-key": "readonly-1",
            },
        )
        self.assertEqual(write_status, 503)
        knowledge_status, _headers, _body = web.handle_dashboard_post(
            "/knowledge/propose",
            b"repo_id=repo-1",
            db_path,
            control_context=context,
            request_headers={
                "host": "127.0.0.1:8765",
                "origin": "http://127.0.0.1:8765",
                "content-type": "application/x-www-form-urlencoded",
                "x-robert-csrf-token": "csrf-test",
            },
        )
        self.assertEqual(knowledge_status, 503)

    def test_reverse_proxy_can_create_and_cancel_with_config_context(self):
        import http.client
        import threading
        from robert_agent.interfaces.web import server as web

        context = web.build_control_context_from_config(
            self._write_acceptance_config(),
            host="0.0.0.0",
            port=8765,
            operator_identity="local-owner",
            public_origin="https://robert.example.com",
        )
        server = web.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            web.make_handler(context.db_path, context),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/session", headers={"Host": "robert.example.com"})
        response = connection.getresponse()
        session = json.loads(response.read())
        self.assertEqual(response.status, 200)
        headers = {
            "Host": "robert.example.com",
            "Origin": "https://robert.example.com",
            "Content-Type": "application/json",
            "X-Robert-CSRF-Token": session["csrf_token"],
            "X-Idempotency-Key": "proxy-create",
        }
        connection.request(
            "POST",
            "/api/work-items",
            json.dumps(
                {
                    "repo_id": "repo:x/y",
                    "title": "Proxy control",
                    "mode": "backlog",
                }
            ),
            headers,
        )
        response = connection.getresponse()
        detail = json.loads(response.read())
        self.assertEqual(response.status, 201, detail)
        path = f"/api/work-items/{detail['work_item_id']}/commands"
        headers["X-Idempotency-Key"] = "proxy-cancel"
        connection.request(
            "POST",
            path,
            json.dumps(
                {
                    "command": "cancel",
                    "expected_version": detail["version"],
                }
            ),
            headers,
        )
        response = connection.getresponse()
        canceled = json.loads(response.read())
        self.assertEqual(response.status, 200, canceled)
        self.assertEqual(canceled["column"], "history")
        self.assertIsNotNone(canceled["canceled_at"])

    def test_web_config_mode_initializes_control_context_and_repos(self):
        from robert_agent.interfaces.web import server as web

        config_path = self._write_acceptance_config()
        context = web.build_control_context_from_config(
            config_path,
            host="127.0.0.1",
            port=9876,
            operator_identity="local-owner",
            csrf_token="fixed-token",
        )

        self.assertTrue(context.writes_enabled)
        self.assertEqual(context.csrf_token, "fixed-token")
        self.assertEqual(context.allowed_repo_ids, frozenset({"repo:x/y"}))
        self.assertEqual(context.allowed_workers, frozenset({"default"}))
        self.assertEqual(context.allowed_origins, frozenset({"http://127.0.0.1:9876"}))
        with closing(sqlite3.connect(context.db_path)) as conn:
            repo = conn.execute("SELECT repo_id, full_name FROM repos WHERE repo_id = 'repo:x/y'").fetchone()
        self.assertEqual(repo, ("repo:x/y", "x/y"))

    def test_web_http_handler_passes_control_context_and_limits_body(self):
        import http.client
        import threading

        from robert_agent.interfaces.web import server as web
        from robert_agent.persistence.schema import database as storage

        db_path = self.root / "handler.sqlite3"
        storage.init_database(db_path)
        server = web.ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = server.server_address[1]
        context = web.ControlContext(
            db_path=str(db_path),
            operator_identity="owner",
            allowed_repo_ids=frozenset(),
            allowed_workers=frozenset(),
            allowed_origins=frozenset({f"http://127.0.0.1:{port}"}),
            csrf_token="handler-token",
            writes_enabled=False,
            write_error="read only",
        )
        server.RequestHandlerClass = web.make_handler(db_path, context)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/api/session")
        response = connection.getresponse()
        session = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(session["csrf_token"], "handler-token")

        connection.request(
            "POST",
            "/api/work-items",
            body=b"x" * (64 * 1024 + 1),
            headers={"content-type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 413)
        connection.close()

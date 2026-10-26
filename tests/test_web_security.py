from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

from robert_agent import storage
from robert_agent.web import (
    build_http_response, build_read_only_control_context, handle_dashboard_post,
    validate_server_options,
)


class WebSecurityTests(unittest.TestCase):
    def test_proxy_origin_passes_host_origin_and_csrf_checks(self):
        context = build_read_only_control_context(
            "unused.sqlite3", host="0.0.0.0", port=8765,
            public_origin="https://robert.example.com/",
        )
        headers = {
            "Host": "robert.example.com", "Origin": "https://robert.example.com",
            "Content-Type": "application/json", "X-Robert-CSRF-Token": context.csrf_token,
        }
        status, _, body = handle_dashboard_post(
            "/api/work-items", b"{}", "unused.sqlite3",
            control_context=context, request_headers=headers,
        )
        self.assertEqual(status, 503, body)
        self.assertIn("--config", json.loads(body)["safe_error"])
        for changes, error in (
            ({"Host": "attacker.example.com"}, "invalid Host"),
            ({"Origin": "https://attacker.example.com"}, "invalid Origin"),
            ({"X-Robert-CSRF-Token": "wrong"}, "invalid CSRF token"),
            ({"Origin": "http://robert.example.com"}, "invalid Origin"),
            ({"Host": "attacker.example.com", "X-Forwarded-Host": "robert.example.com"}, "invalid Host"),
        ):
            with self.subTest(changes=changes):
                status, _, body = handle_dashboard_post(
                    "/api/work-items", b"{}", "unused.sqlite3",
                    control_context=context, request_headers={**headers, **changes},
                )
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["safe_error"], error)

    def test_public_origin_must_be_an_explicit_http_origin(self):
        for origin in (
            "robert.example.com", "ftp://robert.example.com", "https://*.example.com",
            "https://user:password@robert.example.com", "https://robert.example.com/path",
            "https://robert.example.com?query=1", "https://robert.example.com#fragment",
            "https://robert.example.com:invalid", "https://robert.example.com:99999",
            "https://robert.example.com\\attacker", "https://robert.example.com\n",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                build_read_only_control_context(
                    "unused.sqlite3", host="127.0.0.1", port=8765, public_origin=origin,
                )

    def test_public_origin_uses_browser_serialization(self):
        for origin, expected in (
            ("HTTPS://ROBERT.EXAMPLE.COM:443/", "https://robert.example.com"),
            ("http://robert.example.com:80", "http://robert.example.com"),
            ("http://[::1]:9000/", "http://[::1]:9000"),
        ):
            with self.subTest(origin=origin):
                context = build_read_only_control_context(
                    "unused.sqlite3", host="127.0.0.1", port=8765, public_origin=origin,
                )
                self.assertIn(expected, context.allowed_origins)

    def test_custom_public_port_is_preserved(self):
        context = build_read_only_control_context(
            "unused.sqlite3", host="127.0.0.1", port=8765,
            public_origin="https://robert.example.com:9443",
        )
        self.assertIn("https://robert.example.com:9443", context.allowed_origins)
        self.assertIn("http://127.0.0.1:8765", context.allowed_origins)

    def test_readme_logo_remains_available(self):
        logo = (Path(__file__).resolve().parents[1] / "docs/imgs/robert-logo.svg").read_bytes()
        status, headers, body = build_http_response("/assets/robert-logo.svg", "unused.sqlite3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/svg+xml")
        self.assertEqual(body, logo)

    def test_favicon_uses_only_robot_avatar_in_square_canvas(self):
        logo = ET.parse(Path(__file__).resolve().parents[1] / "docs/imgs/robert-logo.svg").getroot()
        for route in ("/assets/robert-favicon.svg", "/favicon.ico"):
            status, headers, body = build_http_response(route, "unused.sqlite3")
            self.assertEqual(status, 200, route)
            self.assertEqual(headers["content-type"], "image/svg+xml")
            icon = ET.fromstring(body)
            _, _, width, height = map(float, icon.attrib["viewBox"].split())
            self.assertEqual(width, height)
            groups = icon.findall("{http://www.w3.org/2000/svg}g")
            self.assertEqual(len(groups), 1)
            self.assertEqual(
                [ET.tostring(shape) for shape in groups[0]],
                [ET.tostring(shape) for shape in logo.find("{http://www.w3.org/2000/svg}g")],
            )

    def test_pages_use_robot_avatar_favicon(self):
        for route in ("/", "/?bucket=history", "/board", "/work", "/operations"):
            status, _, body = build_http_response(route, "unused.sqlite3")
            self.assertEqual(status, 200, route)
            self.assertIn(b'<link rel="icon" type="image/svg+xml" href="/assets/robert-favicon.svg">', body)
            self.assertEqual(body.count(b'rel="icon"'), 1, route)

    def test_artifact_preview_selects_the_requested_historical_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / 'review.sqlite3'
            storage.init_database(db)
            with sqlite3.connect(db) as conn:
                for number in (1, 2):
                    path = root / f'attempt-{number}.log'
                    path.write_text(f'output from attempt {number}')
                    conn.execute(
                        'INSERT INTO artifacts(artifact_id,task_id,artifact_type,path,created_at) VALUES (?,?,?,?,?)',
                        (f'log-{number}', 'task-1', 'worker_stdout', str(path), f'2026-10-2{number}'),
                    )
            url = '/artifact.txt?task_id=task-1&artifact_type=worker_stdout&artifact_id=log-1'
            status, _, body = build_http_response(url, db)
            self.assertEqual(status, 200)
            self.assertIn(b'output from attempt 1', body)
            self.assertNotIn(b'output from attempt 2', body)
            self.assertEqual(build_http_response(url.replace('task-1', 'unrelated-task'), db)[0], 404)
            self.assertEqual(build_http_response(url.replace('log-1', 'unknown'), db)[0], 404)

    def test_default_loopback_read_only_is_allowed(self):
        result = validate_server_options(
            host="127.0.0.1",
            writable=False,
            allow_remote=False,
        )
        self.assertTrue(result["ok"])

    def test_remote_binding_requires_explicit_acknowledgement(self):
        result = validate_server_options(
            host="0.0.0.0",
            writable=False,
            allow_remote=False,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "security_refusal")

    def test_writable_remote_mode_still_requires_csrf(self):
        result = validate_server_options(
            host="0.0.0.0",
            writable=True,
            allow_remote=True,
        )
        self.assertTrue(result["csrf_required"])

    def test_artifact_preview_rejects_traversal_and_unregistered_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "robert.sqlite3"
            storage.init_database(db_path)
            artifact = root / "registered.log"
            artifact.write_text("registered content", encoding="utf-8")
            directory = root / "directory"
            directory.mkdir()
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO artifacts(
                      artifact_id, task_id, artifact_type, path, bytes, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        "artifact-1",
                        "task-1",
                        "worker_stdout",
                        str(artifact),
                        artifact.stat().st_size,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO artifacts(
                      artifact_id, task_id, artifact_type, path, bytes, created_at
                    )
                    VALUES (?, ?, ?, ?, 0, datetime('now'))
                    """,
                    (
                        "artifact-2",
                        "task-1",
                        "directory",
                        str(directory),
                    ),
                )

            encoded_status, _headers, _body = build_http_response(
                "/artifact.txt?task_id=task-1&artifact_type=..%2Fworker_stdout",
                db_path,
            )
            absolute_status, _headers, _body = build_http_response(
                f"/artifact.txt?task_id=task-1&artifact_type={artifact}",
                db_path,
            )
            directory_status, _headers, _body = build_http_response(
                "/artifact.txt?task_id=task-1&artifact_type=directory",
                db_path,
            )

        self.assertEqual(encoded_status, 404)
        self.assertEqual(absolute_status, 404)
        self.assertEqual(directory_status, 404)

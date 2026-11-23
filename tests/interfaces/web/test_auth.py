import base64
from contextlib import contextmanager, redirect_stdout
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest import mock

from robert_agent.interfaces.web import auth, server


def authorization(username="robert", password="test-password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@contextmanager
def running_server(credentials):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    context = server.build_read_only_control_context("unused.sqlite3", host="127.0.0.1", port=httpd.server_port)
    httpd.RequestHandlerClass = server.make_handler("unused.sqlite3", context, basic_auth=credentials)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, context
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class BasicAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "credentials.json"
        auth.write_basic_auth_file(self.path, "robert", "test-password")
        self.credentials = auth.load_basic_auth_file(self.path)

    def request(self, httpd, method, route, token=None, **headers):
        connection = HTTPConnection(*httpd.server_address, timeout=5)
        self.addCleanup(connection.close)
        if token is not None:
            headers["Authorization"] = token
        connection.request(method, route, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_every_page_api_asset_and_post_requires_credentials_before_access(self):
        with (
            running_server(self.credentials) as (httpd, _),
            mock.patch.object(server, "build_http_response") as read,
            mock.patch.object(server, "handle_dashboard_post") as write,
        ):
            for method, route in [
                ("GET", "/"),
                ("GET", "/api/session"),
                ("GET", "/assets/board.js"),
                ("GET", "/artifact.txt?task_id=private"),
                ("POST", "/api/work-items"),
            ]:
                with self.subTest(method=method, route=route):
                    status, headers, body = self.request(httpd, method, route)
                    self.assertEqual(status, 401)
                    self.assertEqual(headers["WWW-Authenticate"], 'Basic realm="Robert", charset="UTF-8"')
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertNotIn(b"test-password", body)
            read.assert_not_called()
            write.assert_not_called()

    def test_invalid_duplicate_and_malformed_credentials_fail_closed(self):
        with running_server(self.credentials) as (httpd, _):
            for value in [
                authorization(password="wrong"),
                authorization(username="other"),
                "Bearer ignored",
                "Basic !!!",
                "Basic " + base64.b64encode(b"missing-colon").decode(),
                "Basic " + base64.b64encode(b"robert:\xff").decode(),
                authorization() + ", " + authorization(),
                "Basic " + "a" * 17000,
            ]:
                with self.subTest(value=value):
                    self.assertEqual(self.request(httpd, "GET", "/", value)[0], 401)
            connection = HTTPConnection(*httpd.server_address, timeout=5)
            self.addCleanup(connection.close)
            connection.putrequest("GET", "/")
            connection.putheader("Authorization", authorization())
            connection.putheader("Authorization", authorization())
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()

    def test_valid_credentials_preserve_host_origin_csrf_and_read_only_guards(self):
        with running_server(self.credentials) as (httpd, context):
            host = f"127.0.0.1:{httpd.server_port}"
            self.assertEqual(self.request(httpd, "GET", "/", authorization(), Host=host)[0], 200)
            self.assertEqual(self.request(httpd, "GET", "/", authorization(), Host="attacker")[0], 403)
            status, _, body = self.request(httpd, "POST", "/api/work-items", authorization(), Host=host)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["safe_error"], "invalid Origin")
            headers = {"Host": host, "Origin": f"http://{host}", "Content-Type": "application/json"}
            status, _, body = self.request(httpd, "POST", "/api/work-items", authorization(), **headers)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["safe_error"], "invalid CSRF token")
            headers["X-Robert-CSRF-Token"] = context.csrf_token
            status, _, _ = self.request(httpd, "POST", "/api/work-items", authorization(), **headers)
            self.assertEqual(status, 503)

    def test_file_contains_salted_hash_and_never_overwrites_existing_credentials(self):
        self.assertNotIn("test-password", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("test-password", repr(self.credentials))
        other = self.path.with_name("other.json")
        auth.write_basic_auth_file(other, "robert", "test-password")
        self.assertNotEqual(self.path.read_text(), other.read_text())
        with self.assertRaises(FileExistsError):
            auth.write_basic_auth_file(self.path, "robert", "changed")
        self.assertTrue(self.credentials.authorized(authorization()))

    def test_unicode_username_and_password_with_colon(self):
        other = self.path.with_name("unicode.json")
        auth.write_basic_auth_file(other, "用户", "pass:秘密")
        self.assertTrue(auth.load_basic_auth_file(other).authorized(authorization("用户", "pass:秘密")))

    def test_missing_insecure_and_invalid_files_are_rejected_without_secret_details(self):
        for content in ["password-secret", "{}", '{"username":"robert","password_hash":"secret"}', "x" * 17000]:
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError) as raised:
                auth.load_basic_auth_file(self.path)
            self.assertNotIn("secret", str(raised.exception))
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            auth.load_basic_auth_file(self.path)
        with self.assertRaises(ValueError):
            auth.load_basic_auth_file(self.path.with_name("missing.json"))

    def test_invalid_usernames_and_empty_password_are_rejected(self):
        for username, password in [("", "pass"), ("a:b", "pass"), ("a\n", "pass"), ("a", "")]:
            with self.subTest(username=username), self.assertRaises(ValueError):
                auth.write_basic_auth_file(self.path.with_name("invalid.json"), username, password)

    def test_symlink_is_rejected_without_following_the_target(self):
        link = self.path.with_name("link.json")
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            auth.load_basic_auth_file(link)

    def test_password_prompt_mismatch_leaves_no_file_and_never_echoes_password(self):
        path = self.path.with_name("new.json")
        with mock.patch.object(auth, "getpass", side_effect=["secret-a", "secret-b"]):
            result = auth.initialize_basic_auth_file(path, "robert")
        self.assertFalse(result["ok"])
        self.assertFalse(path.exists())
        self.assertNotIn("secret", json.dumps(result))

    def test_prompt_creates_verifier_and_does_not_return_plaintext_password(self):
        path = self.path.with_name("new.json")
        with mock.patch.object(auth, "getpass", return_value="secret-password"):
            result = auth.initialize_basic_auth_file(path, "robert")
        self.assertTrue(result["ok"])
        self.assertTrue(auth.load_basic_auth_file(path).authorized(authorization(password="secret-password")))
        self.assertNotIn("secret-password", json.dumps(result))

    def test_requested_invalid_authentication_prevents_server_start_and_database_access(self):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            mock.patch.object(server, "serve") as serve,
            mock.patch.object(server.validate_config, "validate_config") as validate,
        ):
            code = server.main(
                ["--config", "unused.yml", "--basic-auth-file", str(self.path.with_name("missing.json"))]
            )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed_input")
        serve.assert_not_called()
        validate.assert_not_called()

    def test_authentication_is_optional_for_existing_local_server(self):
        with (
            running_server(None) as (httpd, _),
            mock.patch.object(
                server,
                "build_http_response",
                return_value=(200, {"content-type": "text/plain"}, b"existing local server"),
            ),
        ):
            status, _, body = self.request(httpd, "GET", "/", Host=f"127.0.0.1:{httpd.server_port}")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"existing local server")

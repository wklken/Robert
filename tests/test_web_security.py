from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage
from robert_agent.web import build_http_response, validate_server_options


class WebSecurityTests(unittest.TestCase):
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

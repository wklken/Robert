from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage
from robert_agent.web import build_http_response, validate_server_options


class WebSecurityTests(unittest.TestCase):
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

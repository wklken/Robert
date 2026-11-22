from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from robert_agent.interfaces.web.server import ControlContext, build_http_response, handle_dashboard_post
from robert_agent.persistence import database
from robert_agent.persistence.stores import proposals as store
from robert_agent.work.application.web_setup import prepare_repositories
from robert_agent.persistence.schema.database import init_database


class ProposalWebTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.database = Path(workspace.name) / "robert.db"
        init_database(self.database)

    def test_proposals_page_and_collection_are_available_read_only(self):
        status, _, page = build_http_response("/proposals", self.database)
        self.assertEqual(status, 200)
        self.assertIn(b"proposals.js", page)
        status, _, body = build_http_response("/api/proposals", self.database)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"proposals": []})

    def test_unsupported_filter_is_rejected(self):
        status, _, _ = build_http_response("/api/proposals?status=unknown", self.database)
        self.assertEqual(status, 400)

    def test_web_approval_requires_csrf_and_returns_one_durable_decision(self):
        config = {
            "db_path": str(self.database),
            "repos": [
                {
                    "full_name": "a/b",
                    "github_account": "worker",
                    "default_base_branch": "main",
                    "worktree_root": ".",
                    "repo_root": ".",
                }
            ],
        }
        prepare_repositories(config)
        now = "2026-11-22T01:00:00+00:00"
        with closing(database.connect(self.database)) as conn, conn:
            store.create(
                conn,
                {
                    "proposal_id": "p-1",
                    "source_identity": "github:a/b:1",
                    "repo_id": "repo:a/b",
                    "status": "pending",
                    "version": 1,
                    "revision": 1,
                    "source_key": "github:a/b:issue:1",
                    "fingerprint": "abc",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            store.record_revision(
                conn,
                {
                    "proposal_id": "p-1",
                    "revision": 1,
                    "source_json": "{}",
                    "report_json": "{}",
                    "created_at": now,
                },
            )
        context = ControlContext(
            str(self.database),
            "web-operator",
            frozenset({"repo:a/b"}),
            frozenset(),
            frozenset({"http://localhost:8765"}),
            "csrf",
            True,
            None,
            proposal_enabled=True,
        )
        url = "/api/proposals/p-1/decisions"
        body = b'{"action":"approve","expected_version":1}'
        headers = {
            "host": "localhost:8765",
            "origin": "http://localhost:8765",
            "content-type": "application/json",
            "x-idempotency-key": "abc",
        }
        self.assertEqual(handle_dashboard_post(url, body, self.database, context, headers)[0], 403)
        headers["x-robert-csrf-token"] = "csrf"
        for _ in range(2):
            status, _, result = handle_dashboard_post(url, body, self.database, context, headers)
            self.assertEqual(status, 200, result)
            result = json.loads(result)
            self.assertEqual(result["status"], "approved")
            self.assertEqual(len(result["decisions"]), 1)
            self.assertEqual(result["decisions"][0]["actor"], "web-operator")

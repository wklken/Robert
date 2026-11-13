"""Model diagnostics cannot carry credentials into durable state or messages."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from tests.support import TEST_ROOT
from robert_agent.delivery import messages
from robert_agent.persistence import database
from robert_agent.persistence.schema import database as schema
from robert_agent.supervision import decisions, store
from robert_agent.supervision.application import decision_control
from robert_agent.supervision.application.decision_loop import DecisionLoop


class DecisionDiagnosticRedactionTests(unittest.TestCase):
    def test_real_url_credentials_are_withheld_before_persistence_and_notification(self):
        cases = json.loads((TEST_ROOT / "fixtures" / "url_userinfo.json").read_text())
        for case in cases:
            case["error"] = f"Connection failed: https://{case['userinfo']}@proxy.example.org/path"
        self._assert_diagnostics_withheld(cases)

    def test_short_credentials_are_withheld_before_persistence_and_notification(self):
        self._assert_diagnostics_withheld(
            [
                {"name": "short-password", "userinfo": "abcde", "error": "Proxy rejected password=abcde"},
                {"name": "quoted-key", "userinfo": "z!", "error": 'API_KEY="z!" rejected'},
                {"name": "leading-punctuation", "userinfo": "!abc", "error": "Proxy rejected password=!abc"},
                {"name": "placeholder-suffix", "userinfo": "none!", "error": "Proxy rejected password=none!"},
                {"name": "unicode", "userinfo": "短密钥", "error": "Proxy rejected password=短密钥"},
                {"name": "env-prefix", "userinfo": "!abc", "error": "token=$TOKEN|password=!abc"},
            ]
        )

    def _assert_diagnostics_withheld(self, cases):
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as directory:
                db = Path(directory) / "state.db"
                schema.init_database(db)
                now = datetime.now(timezone.utc).isoformat()
                config = {
                    "db_path": str(db),
                    "repos": [{"full_name": "example/project"}],
                    "supervision": {"mode": "decide"},
                }
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute(
                        "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                        "VALUES ('r','example/project','bot','main','/repo','/trees')"
                    )
                    conn.execute(
                        "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('w','r',?,?)",
                        (now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                        "VALUES ('t','w','running',?,?)",
                        (now, now),
                    )
                with closing(database.connect(db)) as conn, conn:
                    request = decisions.packet(decisions.snapshot(conn, "w", config, now), {})
                    store.update_checkpoint(
                        conn,
                        *decisions.RUNNER_KEY,
                        now=now,
                        metadata={
                            "failures": 1,
                            "claim": {"bundle_id": request["bundle_id"], "claimed_at": now, "cooldown_seconds": 120},
                        },
                    )
                error = case["error"]
                profile = {
                    "command": [sys.executable, "-c", f"import sys; sys.stderr.write({error!r}); sys.exit(7)"],
                    "timeout_seconds": 3,
                    "environment_allowlist": [],
                }
                loop = DecisionLoop()
                self.addCleanup(loop.shutdown, config)
                loop._run(config, profile, request)
                outcome = loop.result[1]
                self.assertEqual(outcome["status"], "exited")
                self.assertEqual(outcome["exit_code"], 7)
                self.assertTrue(outcome["stopped"])
                result = loop._finish(config, now)
                self.assertEqual(result["status"], "exited")
                with closing(database.connect(db)) as conn:
                    state = decisions.read(conn, "w")
                    record = store.get_checkpoint(conn, "decision_record", "w", request["bundle_id"])
                    runner = store.get_checkpoint(conn, *decisions.RUNNER_KEY)
                    payload = messages.envelope(conn, "decision-unavailable-" + request["bundle_id"])
                with closing(sqlite3.connect(db)) as conn:
                    # Inspect storage itself, not just redacted query projections.
                    checkpoints = conn.execute("SELECT metadata_json FROM supervision_checkpoints").fetchall()
                    notice = conn.execute("SELECT metadata_json FROM notifications").fetchone()
                for boundary, value in (
                    ("return", outcome),
                    ("state", state),
                    ("record", record),
                    ("runner", runner),
                    ("stored_checkpoints", checkpoints),
                    ("stored_notice", notice),
                    ("envelope", payload),
                    ("status_after_restart", decision_control.status(config)),
                ):
                    with self.subTest(boundary=boundary):
                        self.assertNotIn(case["userinfo"], str(value))
                expected = "Model stderr contained sensitive text; details withheld."
                self.assertEqual(outcome["safe_error"], expected)
                self.assertEqual(state["last_failure"]["safe_error"], expected)
                self.assertEqual(record["metadata"]["last_failure"]["safe_error"], expected)
                self.assertEqual(runner["metadata"]["last_failure"]["safe_error"], expected)
                self.assertNotIn("safe_error", json.loads(notice[0]))
                self.assertNotIn("safe_error", payload)
                self.assertEqual(payload["actions"], ["ack"])
                self.assertIsNone(runner["metadata"]["claim"])

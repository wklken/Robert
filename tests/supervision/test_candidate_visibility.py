from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.persistence.schema import database as schema
from robert_agent.supervision import decisions


NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
EXPIRED = NOW - timedelta(minutes=16)
FUTURE = NOW + timedelta(minutes=1)


class CandidateVisibilityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        db = Path(directory) / "state.db"
        schema.init_database(db)
        self.conn = self.enterContext(closing(sqlite3.connect(db)))
        with self.conn:
            self.conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
                " VALUES ('r','acme/project','bot','main','/repo','/trees')"
            )
            self.conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('w','r',?,?)",
                (NOW.isoformat(), NOW.isoformat()),
            )
        self.config = self.config_with_timeout(3)
        self.previous_config = self.config_with_timeout(4)

    @staticmethod
    def config_with_timeout(seconds):
        return {
            "repos": [{"full_name": "acme/project", "enabled": True}],
            "supervision": {"mode": "decide", "decision": {"command": [sys.executable], "timeout_seconds": seconds}},
        }

    def offer(self, target_id, observed=NOW, *, config=None):
        candidate = decisions.offer(
            self.conn,
            self.config if config is None else config,
            workstream_id="w",
            action="resolve_conflict",
            target_id=target_id,
            facts={"episode_id": target_id},
            now=observed.isoformat(),
        )
        self.assertIsNotNone(candidate, f"offer failed for {target_id}")
        return candidate

    def candidate_ids(self):
        snapshot = decisions.snapshot(self.conn, "w", self.config, NOW.isoformat())
        self.assertIsNotNone(snapshot)
        return [candidate["id"] for candidate in snapshot["candidates"]]

    def test_snapshot_excludes_ineligible_candidates(self):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            withdrawn = self.offer("withdrawn")
            self.assertEqual(self.candidate_ids(), [withdrawn["id"]])
            decisions.withdraw_repairs(self.conn, self.config, "w", NOW.isoformat())
            self.offer("expired", EXPIRED)
            self.offer("future", FUTURE)
            self.offer("old-policy", config=self.previous_config)
            current = self.offer("current")

        self.assertEqual(self.candidate_ids(), [current["id"]])

    def test_current_candidate_survives_ineligible_history(self):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            history = [self.offer(f"withdrawn-{index}") for index in range(36)]
            decisions.withdraw_repairs(self.conn, self.config, "w", NOW.isoformat())
            history.extend(self.offer(f"expired-{index}", EXPIRED) for index in range(36))
            history.extend(self.offer(f"old-policy-{index}", config=self.previous_config) for index in range(36))
            # Refresh the offer that sorts after the stale IDs, using IDs returned by offer.
            last = max(history, key=lambda candidate: candidate["id"])
            current = self.offer(last["target_id"])

        self.assertEqual(self.candidate_ids(), [current["id"]])

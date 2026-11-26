"""Daily inbox upgrades preserve existing proposals and their decision receipts."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from robert_agent.persistence.schema import database as storage
from robert_agent.work.application.commands import CommandContext
from robert_agent.work.application.proposal_commands import decide


LEGACY_PROPOSAL_SCHEMA = """
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY NOT NULL,
  source_identity TEXT NOT NULL UNIQUE,
  repo_id TEXT NOT NULL REFERENCES repos(repo_id),
  status TEXT NOT NULL CHECK (status IN ('evaluated','pending','held','discarded','approved','admitted','blocked')),
  version INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  source_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  blocked_reason TEXT NOT NULL DEFAULT '',
  work_item_id TEXT REFERENCES work_items(work_item_id) ON DELETE SET NULL,
  workstream_id TEXT REFERENCES workstreams(workstream_id) ON DELETE SET NULL,
  task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (version >= revision AND revision >= 0)
);
CREATE INDEX idx_proposals_status_updated ON proposals(status,updated_at,proposal_id);
CREATE TABLE proposal_revisions (
  proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  source_json TEXT NOT NULL,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (proposal_id,revision)
);
CREATE TABLE proposal_decisions (
  proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('approve','hold','discard')),
  actor TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (proposal_id,version),
  UNIQUE (proposal_id,idempotency_key)
);
CREATE TABLE proposal_scans (
  slot TEXT PRIMARY KEY NOT NULL,
  completed_at TEXT NOT NULL
);
"""


class ProposalMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "legacy.sqlite3"
        schema = storage.schema_sql()
        start = schema.index("CREATE TABLE IF NOT EXISTS proposals (")
        end = schema.index("CREATE TABLE IF NOT EXISTS model_usage (")
        schema = schema[:start] + LEGACY_PROPOSAL_SCHEMA + schema[end:]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.executescript(schema)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES('repo-1','example/repo','robot','main','/repo','/trees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) "
                "VALUES('stream-1','repo-1','completed','2026-11-24','2026-11-24')"
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) "
                "VALUES('task-1','stream-1','completed','2026-11-24','2026-11-24')"
            )
            conn.execute(
                "INSERT INTO work_items(work_item_id,repo_id,title,origin_type,workstream_id,creation_idempotency_key,"
                "created_by,created_at,updated_at) VALUES('item-1','repo-1','Existing work','github','stream-1',"
                "'item-create','owner','2026-11-24','2026-11-24')"
            )
            for status in ("pending", "held", "discarded", "approved", "admitted", "blocked", "evaluated"):
                identifier = "p-" + status
                conn.execute(
                    "INSERT INTO proposals(proposal_id,source_identity,repo_id,status,version,revision,source_key,"
                    "fingerprint,created_at,updated_at) VALUES(?,?,'repo-1',?,2,1,?,'fingerprint',?,?)",
                    (identifier, identifier, status, identifier, "2026-11-24T01:00:00Z", "2026-11-24T02:00:00Z"),
                )
                conn.execute(
                    "INSERT INTO proposal_revisions VALUES(?,1,?,?,'2026-11-24T01:00:00Z')",
                    (
                        identifier,
                        json.dumps({"provider": "github", "repository": "example/repo"}),
                        json.dumps({"kind": "implementation", "summary": "Saved report"}),
                    ),
                )
            conn.execute(
                "UPDATE proposals SET work_item_id='item-1',workstream_id='stream-1',task_id='task-1' "
                "WHERE proposal_id='p-admitted'"
            )
            conn.execute(
                "INSERT INTO proposal_decisions VALUES('p-approved',2,1,'approve','owner','old-approval','Approved',"
                "'2026-11-24T02:00:00Z')"
            )
            conn.execute(
                "INSERT INTO proposal_decisions VALUES('p-held',2,1,'hold','owner','old-hold','Wait',"
                "'2026-11-24T02:00:00Z')"
            )
            conn.execute("INSERT INTO proposal_scans VALUES('2026-11-24','2026-11-24T01:00:00Z')")
            self.columns = {
                name: [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
                for name in ("proposals", "proposal_revisions", "proposal_decisions", "proposal_scans")
            }
            self.before = self.snapshot(conn)

    def snapshot(self, conn):
        return {
            name: conn.execute(f"SELECT {','.join(columns)} FROM {name} ORDER BY 1,2").fetchall()
            for name, columns in self.columns.items()
        }

    def test_upgrade_preserves_legacy_rows_receipts_relationships_and_is_repeatable(self):
        for _ in range(2):
            storage.init_database(self.db_path)
            with closing(sqlite3.connect(self.db_path)) as conn:
                self.assertEqual(self.snapshot(conn), self.before)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    conn.execute("SELECT defer_until,investigation_task_id FROM proposals").fetchall(),
                    [(None, None)] * 7,
                )
                self.assertEqual(
                    conn.execute("SELECT options_json FROM proposal_decisions ORDER BY proposal_id").fetchall(),
                    [("{}",), ("{}",)],
                )
                self.assertEqual(conn.execute("SELECT result_json FROM proposal_scans").fetchall(), [("{}",)])
                for table in self.columns:
                    targets = [row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})")]
                    self.assertFalse(any(target.endswith("_legacy") for target in targets))
        context = CommandContext("operator", "owner", frozenset({"repo-1"}), frozenset())
        replay = decide(
            self.db_path,
            "p-approved",
            context=context,
            action="approve",
            expected_version=1,
            idempotency_key="old-approval",
            reason="Approved",
        )
        self.assertEqual((replay["status"], replay["version"]), ("approved", 2))
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(conn.execute("SELECT count(*) FROM proposal_decisions").fetchone()[0], 2)
            conn.execute("DELETE FROM proposals WHERE proposal_id='p-approved'")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM proposal_revisions WHERE proposal_id='p-approved'").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM proposal_decisions WHERE proposal_id='p-approved'").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("SELECT count(*) FROM tasks WHERE task_id='task-1'").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

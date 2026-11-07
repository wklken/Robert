from robert_agent.persistence.schema import review_rounds as review_schema, vote_rounds as vote_schema
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from tests import support  # noqa: F401
from robert_agent.persistence.rounds import review as review_store
from robert_agent.persistence.rounds import vote as vote_store


class RunDirectoryRecoveryTests(unittest.TestCase):
    def test_rollback_orphan_is_not_reused_or_allowed_to_block_retry(self):
        for store, schema, kind in ((review_store, review_schema, "review"), (vote_store, vote_schema, "vote")):
            for stage in ("prepare", kind, "synthesis"):
                with self.subTest(kind=kind, stage=stage), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    db = root / "state.sqlite3"
                    with closing(sqlite3.connect(db)) as conn, conn:
                        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY)")
                        conn.execute("CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY)")
                        conn.execute("INSERT INTO attempts VALUES ('attempt')")
                        schema.initialize(conn)
                        request = store.enqueue(
                            conn,
                            {"full_name": "example/repo"},
                            {
                                "number": 1,
                                "source_type": "issue",
                                "workstream_id": "ws",
                                "event_fingerprint": "trigger",
                            },
                            {},
                            [],
                        )
                    with self.assertRaisesRegex(RuntimeError, "rollback"):
                        with store.connect(db) as conn:
                            old = store.new_run(conn, request["round_id"], stage, "worker", "attempt", root, "hash")
                            Path(old["output_dir"], "result.json").write_text("untrusted orphan result")
                            raise RuntimeError("rollback")
                    with store.connect(db) as conn:
                        new = store.new_run(conn, request["round_id"], stage, "worker", "attempt", root, "hash")
                    self.assertEqual(new["attempt_no"], 1)
                    self.assertNotEqual(old["output_dir"], new["output_dir"])
                    self.assertEqual(list(Path(new["output_dir"]).iterdir()), [])
                    self.assertEqual(Path(old["output_dir"], "result.json").read_text(), "untrusted orphan result")

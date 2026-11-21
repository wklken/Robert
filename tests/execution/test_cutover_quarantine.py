from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

from tests.support.compact_result import CompactResultFixture
from robert_agent.delivery.application import submission
from robert_agent.execution.application import cutover
from robert_agent.execution import spool


class CutoverQuarantineTests(CompactResultFixture):
    def test_archived_invalid_spool_does_not_block_a_valid_pending_result(self):
        db, task_id, attempt_id, _, _ = self.prepare("comment")
        with patch.object(submission, "_record_result_db", side_effect=sqlite3.DatabaseError("disk I/O error")):
            receipt = submission.record_result(db, self.envelope(task_id, attempt_id))
        self.assertEqual(receipt["status"], "deferred")
        archive = spool.directory(db) / "old.invalid"
        archive.write_bytes(b'{"payload":{"attempt_id":"unknown"},"status":"pending"}')
        saved = archive.read_bytes()
        checked = cutover.prepare(db)
        self.assertTrue(checked["ok"], checked)
        self.assertEqual(checked["quarantined_spools"], [{"kind": "archived_invalid_spool", "path": str(archive)}])
        self.assertEqual(submission.replay_pending_results(db)["recorded"], 1)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 1)
        self.assertEqual(archive.read_bytes(), saved)

    def test_pending_legacy_spool_still_stops_cutover_and_preserves_evidence(self):
        db, _, _, _, _ = self.prepare("comment")
        root = spool.directory(db)
        root.mkdir(exist_ok=True)
        pending = root / "legacy.json"
        pending.write_text('{"status":"pending","payload":{}}')
        saved = pending.read_bytes()
        checked = cutover.prepare(db)
        self.assertFalse(checked["ok"], checked)
        self.assertEqual(checked["blockers"], [{"kind": "legacy_or_invalid_spool", "path": str(pending)}])
        self.assertEqual(pending.read_bytes(), saved)

    def test_unreadable_pending_spool_remains_a_shared_blocker(self):
        db, _, _, _, _ = self.prepare("comment")
        root = spool.directory(db)
        root.mkdir(exist_ok=True)
        pending = root / "unreadable.json"
        pending.write_text("preserved unreadable fixture")
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == pending:
                raise PermissionError("fixture permission denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            checked = cutover.prepare(db)
        self.assertFalse(checked["ok"], checked)
        self.assertEqual(checked["blockers"][0]["kind"], "legacy_or_invalid_spool")
        self.assertTrue(pending.exists())

    def test_invalid_archive_cannot_release_a_legacy_running_attempt(self):
        db, _, attempt_id, _, _ = self.prepare("comment")
        root = spool.directory(db)
        root.mkdir(exist_ok=True)
        archive = root / "old.invalid"
        archive.write_text("preserved archive")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET status='running',metadata_json='{}' WHERE attempt_id=?", (attempt_id,))
            checked = cutover.inspect(conn, db)
            self.assertFalse(checked["ok"], checked)
            self.assertEqual(checked["blockers"][0]["kind"], "legacy_running")
            self.assertEqual(
                conn.execute("SELECT status FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0], "running"
            )
        self.assertTrue(archive.exists())

    def test_invalid_archive_does_not_hide_an_unresolved_legacy_publication(self):
        from tests.support.publication_budget import PublicationBudgetFixture

        fixture = PublicationBudgetFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = spool.directory(fixture.db)
        root.mkdir()
        archive = root / "old.invalid"
        archive.write_text("preserved archive")
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.execute("UPDATE worker_results SET metadata_json=json_remove(metadata_json,'$.result_contract')")
            conn.execute("UPDATE tasks SET lifecycle='completed'")
            conn.execute("UPDATE workstreams SET lifecycle='completed',active_task_id=NULL")
            checked = cutover.inspect(conn, fixture.db)
            self.assertFalse(checked["ok"], checked)
            self.assertEqual(checked["blockers"][0]["kind"], "legacy_unresolved_result")
            self.assertEqual(
                conn.execute("SELECT publish_status FROM github_actions WHERE action_id='push'").fetchone()[0],
                "not_published",
            )
        self.assertEqual(archive.read_text(), "preserved archive")

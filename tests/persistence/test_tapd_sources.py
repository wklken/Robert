"""TAPD source rows, first-observation authority and GitHub-only reader filters."""

from pathlib import Path
import json
import tempfile
import unittest

from robert_agent.persistence.queries import github_events as event_queries
from robert_agent.persistence.queries import github_sources as source_queries
from robert_agent.persistence.queries import tapd_scan_state as scan_queries
from robert_agent.persistence.queries import workstreams as workstream_queries
from robert_agent.persistence.schema import database as storage
from robert_agent.persistence.stores import github_events as event_store
from robert_agent.persistence.stores import github_sources as source_store
from robert_agent.persistence.stores import tapd_scan_state as scan_store
from robert_agent.work import queries as work_queries
from tests.support.database import connect

NOW = "2026-11-20T01:00:00+00:00"


def _repo(conn):
    conn.execute(
        "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) "
        "VALUES ('repo-1', 'example/repo', 'robot', 'main', '/repo', '/worktrees')"
    )


def _tapd_source(conn, source_id, *, state="open", metadata=None):
    source_store.upsert_tapd_source(
        conn,
        source_id,
        "repo-1",
        f"tapd:tapd.example/100/story/{source_id}:example/repo",
        "tapd_story",
        "tapd.example/100",
        source_id,
        f"https://tapd.example/100/prong/stories/view/{source_id}",
        "Ticket",
        state,
        NOW,
        json.dumps(metadata or {}),
    )


def _event(conn, event_id, source_id, status="authorized_trigger"):
    return event_store.insert_tapd_event_if_absent(
        conn,
        event_id,
        "repo-1",
        source_id,
        f"tapd-comment:tapd.example/100/{event_id}",
        "comment",
        "alice",
        status,
        NOW,
        json.dumps({"event_fingerprint": event_id}),
    )


def _workstream(conn, workstream_id, source_id, lifecycle="active"):
    conn.execute(
        "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) "
        "VALUES (?, 'repo-1', ?, ?, ?, ?)",
        (workstream_id, source_id, lifecycle, NOW, NOW),
    )


class TapdSourcePersistenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "robert.sqlite3"
        storage.init_database(self.db)
        with connect(self.db) as conn:
            _repo(conn)

    def test_source_refresh_updates_state_and_merges_snapshot(self):
        with connect(self.db) as conn:
            _tapd_source(conn, "11", metadata={"tapd": {"title": "old"}, "kept": True})
            _tapd_source(conn, "11", state="closed", metadata={"tapd": {"title": "new"}})
            row = conn.execute("SELECT provider, state, metadata_json FROM github_sources").fetchone()
        self.assertEqual((row[0], row[1]), ("tapd", "closed"))
        self.assertEqual(json.loads(row[2]), {"tapd": {"title": "new"}, "kept": True})

    def test_event_authority_is_fixed_at_first_observation(self):
        with connect(self.db) as conn:
            _tapd_source(conn, "11")
            self.assertEqual(_event(conn, "e1", "11", "ignored_context"), 1)
            self.assertEqual(_event(conn, "e1", "11", "authorized_trigger"), 0)
            status = conn.execute("SELECT authorization_status, actor_kind FROM github_events").fetchone()
            found = event_queries.find_existing_fingerprints(conn, ["tapd-comment:tapd.example/100/e1", "other"])
        self.assertEqual(tuple(status), ("ignored_context", "tapd_user"))
        self.assertEqual([row[0] for row in found], ["tapd-comment:tapd.example/100/e1"])

    def test_pending_triggers_exclude_linked_disposed_and_reclassified_events(self):
        with connect(self.db) as conn:
            _tapd_source(conn, "11")
            for event_id in ("linked", "disposed", "deferred", "dropped", "pending", "context"):
                _event(conn, event_id, "11", "ignored_context" if event_id == "context" else "authorized_trigger")
            _workstream(conn, "w1", "11")
            conn.execute(
                "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) "
                "VALUES ('t1', 'w1', 'queued', ?, ?)",
                (NOW, NOW),
            )
            conn.execute("INSERT INTO task_events VALUES ('t1', 'linked', 'trigger', ?)", (NOW,))
            for event_id, disposition in (("disposed", "explicitly_ignored"), ("deferred", "deferred")):
                conn.execute(
                    "INSERT INTO supervision_checkpoints(scope_type, scope_id, rule_id, updated_at, metadata_json) "
                    "VALUES ('github_event', ?, 'admission', ?, ?)",
                    (event_id, NOW, json.dumps({"disposition": disposition})),
                )
            event_store.record_tapd_authorization(conn, "dropped", "ignored_untrusted_trigger")
            pending = [row[0] for row in event_queries.find_tapd_pending_triggers(conn, "repo-1", 10)]
        self.assertEqual(pending, ["deferred", "pending"])

    def test_tracked_sources_cover_unfinished_work_and_pending_requests_only(self):
        with connect(self.db) as conn:
            for source_id in ("active", "waiting", "done", "queued", "ended", "idle"):
                _tapd_source(conn, source_id, state="closed" if source_id == "ended" else "open")
            _workstream(conn, "w-active", "active")
            _workstream(conn, "w-waiting", "waiting", "waiting_for_user")
            _workstream(conn, "w-done", "done", "completed")
            _workstream(conn, "w-ended", "ended")
            _event(conn, "q1", "queued")
            tracked = [row[4] for row in source_queries.find_tapd_tracked_sources(conn, "tapd.example/100")]
        self.assertEqual(tracked, ["active", "queued", "waiting"])

    def test_github_readers_expose_provider_and_skip_tapd_recovery_events(self):
        with connect(self.db) as conn:
            _tapd_source(conn, "11")
            _workstream(conn, "w1", "11", "completed")
            event_store.insert_tapd_event_if_absent(
                conn,
                "e1",
                "repo-1",
                "11",
                "tapd-comment:tapd.example/100/e1",
                "comment",
                "alice",
                "accepted_context",
                NOW,
                json.dumps({"drives_execution": True}),
            )
            conn.execute("UPDATE workstreams SET lifecycle = 'active', active_task_id = 't1'")
            rows = workstream_queries.find_for_remote_source_rows(conn, "repo-1")
            conn.execute("UPDATE workstreams SET lifecycle = 'completed', active_task_id = NULL")
            recovery = event_queries.scan_repo_filtered(conn, ["alice"], "repo-1", "robot")
            sources = work_queries.sources_for_task(conn, "w1", [{"provider": "tapd"}])
        self.assertEqual([tuple(row)[6:] for row in rows], [("tapd", "open")])
        self.assertEqual(recovery, [])
        self.assertEqual(
            sources,
            [
                {
                    "source_type": "tapd_story",
                    "provider": "tapd",
                    "project_key": "tapd.example/100",
                    "external_id": "11",
                }
            ],
        )

    def test_scan_watermark_round_trips_per_project(self):
        with connect(self.db) as conn:
            self.assertEqual(scan_queries.find_watermark(conn, "tapd.example/100"), [])
            scan_store.save_watermark(conn, "tapd.example/100", "2026-11-20 09:00:00", "7", NOW)
            scan_store.save_watermark(conn, "tapd.example/100", "2026-11-20 09:05:00", "9", NOW)
            rows = scan_queries.find_watermark(conn, "tapd.example/100")
        self.assertEqual([tuple(row) for row in rows], [("2026-11-20 09:05:00", "9")])


if __name__ == "__main__":
    unittest.main()

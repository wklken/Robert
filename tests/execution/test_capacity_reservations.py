from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent.execution import capacity
from robert_agent.persistence.rounds import review, vote
from robert_agent.persistence.schema import database as schema


NOW = "2026-11-08T12:00:00+00:00"
REPO = "repo:acme/project"
OTHER_REPO = "repo:acme/other"


class CapacityReservationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def connection(self, name="state"):
        db = self.root / f"{name}.db"
        schema.init_database(db)
        conn = self.enterContext(closing(sqlite3.connect(db)))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executemany(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)"
            " VALUES (?,?,'bot','main','/repo','/trees')",
            [(REPO, "acme/project"), (OTHER_REPO, "acme/other")],
        )
        return conn

    def add_task(self, conn, repo_id, *, pid=None):
        stream_id = f"stream:{repo_id}"
        task_id = f"task:{repo_id}"
        attempt_id = f"attempt:{repo_id}:1"
        conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES (?,?,?,?)",
            (stream_id, repo_id, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES (?,?,?,?,?)",
            (task_id, stream_id, "running" if pid else "queued", NOW, NOW),
        )
        conn.execute("UPDATE workstreams SET active_task_id=? WHERE workstream_id=?", (task_id, stream_id))
        conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES (?,?,1,?,?)",
            (
                attempt_id,
                task_id,
                "running" if pid else "prepared",
                json.dumps({"dispatch": {"pid": pid}} if pid else {}),
            ),
        )
        conn.execute(
            "INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at)"
            " VALUES ('task',?,'human_request','active',?,?)",
            (task_id, NOW, NOW),
        )
        return stream_id, task_id, attempt_id

    def assert_counts(self, conn, *, repo, other=0):
        self.assertEqual(
            {
                "repo": capacity.running_attempt_count(conn, REPO),
                "other repo": capacity.running_attempt_count(conn, OTHER_REPO),
                "global": capacity.running_attempt_count_for_repos(conn, [REPO, OTHER_REPO]),
            },
            {"repo": repo, "other repo": other, "global": repo + other},
        )

    def test_prepared_attempt_without_reservation_does_not_add_to_running_count(self):
        conn = self.connection()
        with conn:
            self.add_task(conn, REPO)
            self.add_task(conn, OTHER_REPO, pid=100)

        self.assert_counts(conn, repo=0, other=1)

    def test_prepared_launch_without_captured_identity_reserves_capacity(self):
        conn = self.connection()
        with conn:
            _, task_id, attempt_id = self.add_task(conn, REPO)
            self.add_task(conn, OTHER_REPO)
            conn.execute(
                "UPDATE execution_subjects SET metadata_json=? WHERE subject_type='task' AND subject_id=?",
                (json.dumps({"dispatch_inflight": attempt_id}), task_id),
            )

        self.assert_counts(conn, repo=1)

    def test_native_round_reservation_survives_coordinator_recovery(self):
        for store, stage, status in ((review, "review", "reviewing"), (vote, "vote", "voting")):
            with self.subTest(stage=stage):
                conn = self.connection(stage)
                with conn:
                    stream_id, task_id, coordinator = self.add_task(conn, REPO, pid=100)
                    session = store.enqueue(
                        conn,
                        {"full_name": "acme/project"},
                        {
                            "event_fingerprint": "request-round",
                            "number": 1,
                            "source_type": "pr",
                            "workstream_id": stream_id,
                        },
                        {},
                        [],
                    )
                    round_id = session["round_id"]
                    store.update(conn, round_id, task_id=task_id, status=status)
                    children = []
                    for pid in (101, 102):
                        run = store.new_run(
                            conn, round_id, stage, f"worker-{pid}", coordinator, self.root, "prompt-sha"
                        )
                        run_id = run["run_id"]
                        store.update_run(conn, run_id, status="waiting")
                        self.assertTrue(store.reserve_slot(conn, run_id))
                        store.update_run(conn, run_id, status="running", dispatch_json=json.dumps({"pid": pid}))
                        children.append(run_id)
                with self.subTest(state="running coordinator with two children"):
                    self.assert_counts(conn, repo=1)

                with conn:
                    conn.execute(
                        "UPDATE attempts SET status='failed',finished_at=? WHERE attempt_id=?", (NOW, coordinator)
                    )
                    conn.execute(
                        "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('successor',?,2,'prepared')",
                        (task_id,),
                    )
                with self.subTest(state="prepared successor with two reserved children"):
                    self.assert_counts(conn, repo=1)

                with conn:
                    store.update_run(conn, children[0], status="interrupted")
                    store.release_slot(conn, children[0])
                with self.subTest(state="one child remains reserved"):
                    self.assert_counts(conn, repo=1)

                with conn:
                    store.update_run(conn, children[1], status="interrupted")
                    store.release_slot(conn, children[1])
                with self.subTest(state="all children released"):
                    self.assert_counts(conn, repo=0)

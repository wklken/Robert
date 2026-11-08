"""Coordinator recovery reuses its occupied slot while unrelated work waits."""

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from robert_agent.configuration.command import validate_config
from robert_agent.execution import capacity
from robert_agent.execution.application import dispatch, readiness
from robert_agent.persistence.rounds import review, vote
from tests.support.review_coordinator import ReviewCoordinatorFixture
from tests.support.vote_coordinator import VoteCoordinatorFixture


class NativeCapacityAdmissionTests(unittest.TestCase):
    def test_prepared_successor_can_reuse_only_its_own_round_slot_at_capacity(self):
        for fixture_type, store, stage in (
            (ReviewCoordinatorFixture, review, "review"),
            (VoteCoordinatorFixture, vote, "vote"),
        ):
            with self.subTest(stage=stage):
                fixture = fixture_type()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                base = fixture.fixture if stage == "vote" else fixture
                base.cycle([fixture.event()])
                task_id, old = base.current()
                config = validate_config(base.config_path, skip_external=True)
                config["max_concurrency"] = 1
                config["repos"][0]["max_concurrency"] = 1
                repo_id = "repo:example/backend"
                with closing(sqlite3.connect(base.db)) as conn, conn:
                    session = store.session(conn, task_id=task_id)
                    store.update(conn, session["round_id"], status="reviewing" if stage == "review" else "voting")
                    child = store.new_run(conn, session["round_id"], stage, "codex", old, base.root, "prompt")
                    store.update_run(conn, child["run_id"], status="waiting")
                    self.assertTrue(store.reserve_slot(conn, child["run_id"]))
                    store.update_run(conn, child["run_id"], status="running")
                    conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (old,))
                    conn.execute(
                        "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES('successor',?,2,'prepared',?)",
                        (task_id, json.dumps({"result_contract": "compact-v1"})),
                    )
                    conn.execute(
                        "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,bytes,created_at) SELECT 'successor-prompt',task_id,'successor',artifact_type,path,bytes,created_at FROM artifacts WHERE attempt_id=? AND artifact_type='prompt'",
                        (old,),
                    )
                    self.assertEqual(capacity.running_attempt_count(conn, repo_id), 1)
                    self.assertTrue(readiness.has_local_work(conn, [repo_id], config_result=config))
                    queue = []
                    self.assertEqual(dispatch.queue_prepared_attempts(conn, repo_id, config["repos"][0], queue, 0), 0)
                    self.assertEqual([entry["task_info"]["attempt_id"] for entry in queue], ["successor"])
                with patch.object(
                    dispatch.dispatch,
                    "dispatch_worker",
                    return_value={
                        "ok": True,
                        "status": "running",
                        "pid": 1073741824,
                        "process_exited": True,
                        "returncode": 0,
                    },
                ) as launch:
                    dispatch.dispatch_queued(
                        base.db,
                        [repo_id],
                        queue,
                        False,
                        1,
                        workers=config["workers"],
                        default_worker=config["default_worker"],
                        config_result=config,
                    )
                    self.assertEqual(launch.call_count, 1)
                with closing(sqlite3.connect(base.db)) as conn:
                    self.assertEqual(capacity.running_attempt_count(conn, repo_id), 1)
                    self.assertEqual(len(store.runs(conn, session["round_id"])), 1)

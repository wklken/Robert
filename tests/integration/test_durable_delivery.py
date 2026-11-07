"""Acceptance gaps left by the first reliability delivery."""

from tests.support.durable_result import DurableResultFixture

from robert_agent.delivery import queries as publication_queries

from contextlib import closing

import sqlite3

import unittest


from tests.support import PACKAGE_ROOT  # noqa: F401


class DeliveryAndRecoveryTests(unittest.TestCase):
    def test_old_stopped_actions_cannot_starve_later_ready_result(self):
        from tests.support.publication_budget import PublicationBudgetFixture

        fixture = PublicationBudgetFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE github_actions SET metadata_json=json_set(metadata_json,'$.publish.retryable',0)"
            )
            for i in range(25):
                conn.execute(
                    "INSERT INTO github_actions SELECT ?,result_id,task_id,action_type,target_url,external_id,audit_status,publish_status,created_at,metadata_json FROM github_actions WHERE action_id='push'",
                    ("stopped-" + str(i),),
                )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a2','t',2,'completed')"
            )
            conn.execute(
                "INSERT INTO worker_results SELECT 'r2',task_id,'a2',output_type,consumed_event_fingerprints_json,verification_json,handoff,created_at,metadata_json FROM worker_results WHERE result_id='r'"
            )
            conn.execute(
                "INSERT INTO github_actions SELECT 'healthy','r2',task_id,action_type,target_url,external_id,audit_status,publish_status,'zzz',json_remove(metadata_json,'$.publish') FROM github_actions WHERE action_id='push'"
            )
            ready = publication_queries.ready_actions(conn, 20)
            self.assertEqual([row["action_id"] for row in ready], ["healthy"])

    def test_commit_then_process_death_replays_without_duplicate_actions(self):
        import json
        import subprocess
        import sys
        from pathlib import Path
        from robert_agent.delivery.application import submission as result

        fixture = DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        payload = Path(fixture.db).with_suffix(".payload.json")
        payload.write_text(json.dumps(fixture.payload))
        program = """import json,os,sys
from robert_agent.execution import spool as result_spool
from robert_agent.delivery.application import submission as result
p=json.load(open(sys.argv[2]))
def writer(db,payload,notifier=None):
 response=result._record_result_db(db,payload,notifier=notifier)
 assert response['ok'], response
 os._exit(73)
result_spool.submit(sys.argv[1],p,writer,prepare=result._prepare,lookup=result._lookup)
"""
        child = subprocess.run(
            [sys.executable, "-c", program, str(fixture.db), str(payload)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(child.returncode, 73, child.stderr)
        self.assertEqual(result.replay_pending_results(fixture.db)["recorded"], 1)
        with closing(sqlite3.connect(fixture.db)) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM wakeups").fetchone()[0], 1
            )

    def test_30_second_write_lock_preserves_offline_result(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        import time
        from robert_agent.delivery.application import submission as result

        fixture = DurableResultFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        payload = Path(fixture.db).with_suffix(".payload.json")
        payload.write_text(json.dumps(fixture.payload))
        program = """import json,sys
from robert_agent.delivery.application import submission as result
print(json.dumps(result.record_result(sys.argv[1],json.load(open(sys.argv[2])))))
"""
        with closing(sqlite3.connect(fixture.db)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            child = subprocess.run(
                [sys.executable, "-c", program, str(fixture.db), str(payload)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout)["status"], "deferred")
            time.sleep(max(0, 30 - (time.monotonic() - start)))
            blocker.rollback()
        self.assertEqual(result.replay_pending_results(fixture.db)["recorded"], 1)
        self.assertEqual(result.replay_pending_results(fixture.db)["recorded"], 0)
        with closing(sqlite3.connect(fixture.db)) as conn:
            timing = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )
            from robert_agent.readmodels.execution_metrics import _seconds

            self.assertGreaterEqual(
                _seconds(timing["result_saved_at"], timing["result_ingested_at"]), 29
            )

from robert_agent.supervision.application import checks as supervision_checks
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.support import PACKAGE_ROOT  # noqa: F401
from robert_agent.adapters.sqlite import database as storage
from robert_agent.supervision.application import observation as supervision_observe
from robert_agent.supervision import rules as supervision_rules


NOW = datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
OLD = "2026-11-06T09:00:00+00:00"
LATER = "2026-11-06T11:00:00+00:00"


def task_facts(**changes):
    return {
        "task_id": "task",
        "lifecycle": "running",
        "runnable": True,
        "attempt_id": "attempt",
        "attempt_status": "running",
        "process_status": "alive_same",
        "started_at": OLD,
        "heartbeat_at": OLD,
        **changes,
    }


def evaluate(rule, facts):
    return next(
        row
        for row in supervision_rules.evaluate_task(facts, now=NOW)
        if row["rule_id"] == rule
    )


class SupervisionProgressRulesTests(unittest.TestCase):
    def test_expired_daemon_lease_reports_missing_progress_after_grace(self):
        facts = {
            "scope_id": "daemon",
            "status": "expired",
            "lease_id": "lease",
            "lease_expires_at": OLD,
        }
        row = supervision_rules.evaluate_controller(facts, now=NOW)
        self.assertEqual(
            (row["classification"], row["reason_code"]),
            ("confirmed", "controller_progress_stalled"),
        )
        self.assertNotIn("positive_evidence", row)

    def test_stale_work_starts_eligibility_clock_when_backoff_ends(self):
        facts = task_facts(
            pending_action_id="action",
            publication_eligible=True,
            action_created_at=OLD,
            publication_retry_at=LATER,
        )
        waiting = evaluate("publication_stalled", facts)
        self.assertIsNone(waiting["eligible_since"])
        facts.update(
            publication_retry_at=OLD, checkpoints={"publication_stalled": waiting}
        )
        ready = evaluate("publication_stalled", facts)
        self.assertEqual(ready["classification"], "expected_wait")
        self.assertEqual(ready["eligible_since"], NOW.isoformat())

    def test_repeated_failed_activity_does_not_reset_worker_progress(self):
        facts = task_facts(
            milestones=[["review_result", "known-digest"]],
            milestone_observed_at=OLD,
            command_failures=2,
        )
        first = evaluate("worker_no_progress", facts)
        facts.update(command_failures=9, heartbeat_at=NOW.isoformat())
        second = evaluate("worker_no_progress", facts)
        self.assertEqual(second["progress_token"], first["progress_token"])
        facts["milestones"].append(["review_report", "new-digest"])
        self.assertNotEqual(
            evaluate("worker_no_progress", facts)["progress_token"],
            first["progress_token"],
        )

    def test_prepared_capacity_wait_does_not_become_ready_stall(self):
        row = evaluate(
            "ready_not_dispatched",
            task_facts(
                lifecycle="queued",
                attempt_status="prepared",
                dispatch_block="repository_capacity",
                has_prompt=True,
                has_worktree=True,
            ),
        )
        self.assertEqual(row["classification"], "expected_wait")
        self.assertEqual(row["reason_code"], "repository_capacity")
        self.assertIsNone(row["eligible_since"])

    def test_complete_delivery_with_a_missing_planned_action_is_confirmed(self):
        result = evaluate(
            "delivery_inconsistent",
            task_facts(
                lifecycle="completed",
                attempt_status="completed",
                result_id="result",
                delivery_status="inconsistent",
                delivery_reason="planned_actions_do_not_match",
                planned_action_count=2,
                action_count=1,
            ),
        )
        self.assertEqual(result["classification"], "confirmed")
        self.assertNotIn("positive_evidence", result)


class _SupervisionDatabaseFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "state.db"
        storage.init_database(self.db)
        self.execute(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','default','main','/tmp','/tmp')"
        )
        self.execute(
            "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number) VALUES('source','repo','example/repo#1','issue',1)"
        )

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(sql, params)

    def receipt(self, key, **fields):
        metadata = {
            "received_at": OLD,
            "input_generation": "generation",
            "source_id": "source",
            "repo_id": "repo",
            "lineage_key": "lineage",
            **fields,
        }
        self.execute(
            "INSERT INTO supervision_checkpoints(scope_type,scope_id,rule_id,lineage_key,updated_at,metadata_json) VALUES('github_event',?,'admission','lineage',?,?)",
            (key, OLD, json.dumps(metadata)),
        )


class SupervisionAdmissionObservationTests(_SupervisionDatabaseFixture):
    def test_missing_and_deferred_dispositions_are_distinct_and_never_claim_progress(
        self,
    ):
        self.receipt("missing")
        self.receipt(
            "deferred", disposition="deferred", consumer="github_route", due_at=LATER
        )
        rows = supervision_observe.collect(self.db, now=NOW)["observations"]
        by_scope = {row["scope_id"]: row for row in rows}
        self.assertEqual(by_scope["missing"]["rule_id"], "accepted_input_unowned")
        self.assertEqual(
            by_scope["missing"]["evidence"]["input_generation"], "generation"
        )
        self.assertNotIn("positive_evidence", by_scope["missing"])
        self.assertEqual(by_scope["deferred"]["classification"], "expected_wait")
        self.assertEqual(by_scope["deferred"]["next_check_at"], LATER)

    def test_admission_and_task_pages_both_advance_at_batch_size_one(self):
        for key in ("a", "b", "c"):
            self.receipt("receipt-" + key)
            self.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES(?,'repo',?,?,?)",
                ("stream-" + key, key, OLD, OLD),
            )
            self.execute(
                "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES(?,?,'running',?,?)",
                (key, "stream-" + key, OLD, OLD),
            )
        seen = []
        cursor = ""
        for _ in range(6):
            page = supervision_observe.collect(self.db, now=NOW, limit=1, cursor=cursor)
            scopes = {
                row["scope_id"]
                for row in page["observations"]
                if row["scope_type"] != "controller"
            }
            self.assertEqual(len(scopes), 1)
            seen.extend(scopes)
            cursor = page["cursor"]
        self.assertTrue(page["coverage_complete"])
        self.assertTrue(any(scope.startswith("receipt-") for scope in seen[:2]))
        self.assertEqual(
            set(seen), {"a", "b", "c", "receipt-a", "receipt-b", "receipt-c"}
        )

    def test_admission_receipt_change_invalidates_decisive_snapshot(self):
        self.receipt("missing")
        row = next(
            item
            for item in supervision_observe.collect(self.db, now=NOW)["observations"]
            if item["scope_id"] == "missing"
        )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertTrue(supervision_observe.is_current(conn, row))
        self.execute("""UPDATE supervision_checkpoints SET metadata_json=json_set(metadata_json,'$.disposition','explicitly_ignored')
            WHERE scope_type='github_event' AND scope_id='missing' AND rule_id='admission'""")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertFalse(supervision_observe.is_current(conn, row))


class SupervisionClosureTests(_SupervisionDatabaseFixture):
    def task(self, key, lifecycle, *, parent=None, lineage="lineage"):
        self.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at) VALUES(?,'repo',?,?,?)",
            ("stream-" + key, key, OLD, OLD),
        )
        self.execute(
            """INSERT INTO tasks(task_id,workstream_id,lifecycle,parent_task_id,route_id,expected_output,
            created_at,updated_at,metadata_json) VALUES(?,?,?,?,'local-result','local_result',?,?,json_object('supervision_lineage_key',?))""",
            (key, "stream-" + key, lifecycle, parent, OLD, OLD, lineage),
        )

    def check(self, now, **config):
        return supervision_checks.check(
            {
                "db_path": str(self.db),
                "data_dir": str(self.db.parent),
                "repos": [{"full_name": "example/repo"}],
                **config,
            },
            record=True,
            now=now,
        )

    def daemon(self):
        self.execute(
            "INSERT INTO daemon_runs(daemon_run_id,owner_id,config_path,status,started_at) VALUES('run','run','config','running',?)",
            (OLD,),
        )
        self.execute(
            """INSERT INTO leases(lease_id,resource_type,resource_key,owner_id,status,acquired_at,expires_at,heartbeat_at)
            VALUES('lease','daemon',?,'run','active',?,'2026-11-06T20:00:00+00:00',?)""",
            ("database:" + str(self.db.resolve()), OLD, NOW.isoformat()),
        )

    def successful_local(self):
        self.execute(
            "INSERT INTO daemon_events(daemon_event_id,daemon_run_id,event_type,status,created_at) VALUES('local','run','local_drain_completed','ok','2026-11-06T10:15:00+00:00')"
        )

    def incidents(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT scope_id,rule_id,status,resolution_evidence_json FROM supervision_incidents ORDER BY scope_id"
            ).fetchall()

    def test_ready_queue_opens_incident_and_notice_only_after_current_local_opportunity(
        self,
    ):
        self.daemon()
        self.task("queued", "queued")
        self.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','queued',1,'prepared')"
        )
        self.execute(
            "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at) VALUES('prompt','queued','attempt','prompt',?,?)",
            (str(self.db.parent / "prompt"), OLD),
        )
        self.assertTrue(self.check(NOW.isoformat())["ok"])
        self.assertTrue(self.check("2026-11-06T10:10:00+00:00")["ok"])
        self.assertEqual(self.incidents(), [])
        self.successful_local()
        self.assertTrue(self.check(LATER)["ok"])
        self.assertEqual(
            self.incidents(), [("queued", "ready_not_dispatched", "open", "{}")]
        )
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE task_id='queued' AND notification_type='supervision_incident'"
                ).fetchone()[0],
                1,
            )

    def test_ready_incident_closes_when_exact_attempt_completes_between_checks(self):
        self.daemon()
        self.task("queued", "queued")
        self.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','queued',1,'prepared')"
        )
        self.execute(
            """INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)
            VALUES('prompt','queued','attempt','prompt','/tmp/fixture',?)""",
            (OLD,),
        )
        self.assertTrue(self.check(NOW.isoformat())["ok"])
        self.successful_local()
        self.assertTrue(self.check(LATER)["ok"])
        self.assertEqual(
            self.incidents(), [("queued", "ready_not_dispatched", "open", "{}")]
        )

        self.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id='queued'")
        self.execute(
            "UPDATE attempts SET status='completed' WHERE attempt_id='attempt'"
        )
        self.execute(
            """INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json)
            VALUES('result','queued','attempt','local_result',?,json_object('audit',json_object('status','accepted'),
            'normalized_payload',json_object('planned_github_actions',json_array())))""",
            (LATER,),
        )
        self.assertTrue(self.check("2026-11-06T11:10:00+00:00")["ok"])
        self.assertEqual(
            self.incidents(), [("queued", "ready_not_dispatched", "open", "{}")]
        )

        occurred_at = "2026-11-06T11:15:00+00:00"
        self.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('other','queued',2,'completed')"
        )
        self.execute(
            """UPDATE worker_results SET attempt_id='other',
            metadata_json=json_set(metadata_json,'$.audit.audited_at',?) WHERE result_id='result'""",
            (occurred_at,),
        )
        self.assertTrue(self.check("2026-11-06T11:20:00+00:00")["ok"])
        self.assertEqual(
            self.incidents(), [("queued", "ready_not_dispatched", "open", "{}")]
        )

        self.execute(
            "UPDATE worker_results SET attempt_id='attempt' WHERE result_id='result'"
        )
        self.execute("DELETE FROM attempts WHERE attempt_id='other'")
        self.assertTrue(self.check("2026-11-06T11:30:00+00:00")["ok"])
        [(scope, rule, state, evidence)] = self.incidents()
        self.assertEqual(
            (scope, rule, state), ("queued", "ready_not_dispatched", "resolved")
        )
        self.assertEqual(json.loads(evidence)["attempt_id"], "attempt")
        self.assertEqual(json.loads(evidence)["result_id"], "result")
        self.assertEqual(json.loads(evidence)["occurred_at"], occurred_at)

    def test_ready_queue_capacity_wait_is_not_reported_as_stall(self):
        self.daemon()
        self.task("queued", "queued")
        self.task("occupies", "running")
        self.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','queued',1,'prepared'),('worker','occupies',1,'running')"
        )
        self.execute(
            "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at) VALUES('prompt','queued','attempt','prompt','/tmp/fixture',?)",
            (OLD,),
        )
        self.assertTrue(self.check(NOW.isoformat(), max_concurrency=1)["ok"])
        self.successful_local()
        self.assertTrue(self.check(LATER, max_concurrency=1)["ok"])
        self.assertEqual(self.incidents(), [])

    def test_failure_resolves_only_from_bound_completed_successor_with_domain_time(
        self,
    ):
        self.task("failed", "failed")
        self.assertTrue(self.check(NOW.isoformat())["ok"])
        self.assertEqual(
            self.incidents(), [("failed", "execution_failed", "open", "{}")]
        )
        self.task("unrelated", "completed")
        self.task("successor", "completed", parent="failed")
        for key in ("unrelated", "successor"):
            self.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES(?,?,1,'completed')",
                (key, key),
            )
            self.execute(
                """INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json)
                VALUES(?,?,?,'local_result',?,json_object('audit',json_object('status','accepted'),
                    'normalized_payload',json_object('planned_github_actions',json_array())))""",
                (key, key, key, OLD),
            )
        occurred_at = "2026-11-06T10:25:00+00:00"
        self.execute(
            "UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.audit.audited_at',?) WHERE result_id='unrelated'",
            (occurred_at,),
        )
        self.assertTrue(self.check("2026-11-06T10:35:00+00:00")["ok"])
        self.assertEqual(
            self.incidents(), [("failed", "execution_failed", "open", "{}")]
        )
        self.execute(
            "UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.audit.audited_at',?) WHERE result_id='successor'",
            (occurred_at,),
        )
        self.assertTrue(self.check(LATER)["ok"])
        [(scope, rule, state, evidence)] = self.incidents()
        self.assertEqual(
            (scope, rule, state), ("failed", "execution_failed", "resolved")
        )
        self.assertEqual(json.loads(evidence)["successor_task_id"], "successor")
        self.assertEqual(json.loads(evidence)["occurred_at"], occurred_at)
        self.assertTrue(self.check("2026-11-06T11:05:00+00:00")["ok"])
        self.assertEqual(self.incidents(), [(scope, rule, state, evidence)])

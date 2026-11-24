"""Terminal task incidents are dismissed only after ownership/effects are settled."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robert_agent.persistence.schema import database as storage
from robert_agent.supervision.application import checks
from robert_agent.supervision import store

NOW = "2026-11-21T10:00:00+00:00"


class IncidentRetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "state.db"
        storage.init_database(self.db)
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.config = {"db_path": str(self.db), "data_dir": temporary.name}
        self.conn.execute(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES('repo','example/repo','bot','main','/repo','/trees')"
        )
        self.conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES('stream','repo','canceled',?,?)",
            (NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at,metadata_json) VALUES('task','stream','canceled',?,?,?)",
            (NOW, NOW, json.dumps({"supervision_lineage_key": "lineage"})),
        )
        store.apply_observations(
            self.conn,
            [
                {
                    "scope_type": "task",
                    "scope_id": "task",
                    "rule_id": "execution_failed",
                    "lineage_key": "lineage",
                    "classification": "confirmed",
                    "reason_code": "execution_failed",
                    "severity": "error",
                    "evidence": {},
                    "expected_progress": {},
                }
            ],
            NOW,
        )
        self.conn.commit()

    def run_check(self, **kwargs):
        result = checks.check(self.config, now=NOW, **kwargs)
        self.assertTrue(result["ok"], result)
        return result

    def active(self):
        return store.list_incidents(self.conn, status="active")

    def test_canceled_and_ignored_are_dismissed_with_terminal_evidence(self):
        for lifecycle in ("canceled", "ignored"):
            with self.subTest(lifecycle=lifecycle):
                self.conn.execute("UPDATE tasks SET lifecycle=?", (lifecycle,))
                self.conn.execute("UPDATE supervision_incidents SET status='open',resolved_at=NULL")
                self.conn.commit()
                self.assertEqual(self.run_check()["changes"], [])
                self.assertEqual(len(self.active()), 1)
                result = self.run_check(record=True)
                self.assertEqual([c["transition"] for c in result["changes"]], ["dismissed"])
                item = store.list_incidents(self.conn)[0]
                self.assertEqual(item["status"], "dismissed")
                self.assertEqual(item["resolution_evidence"]["task_lifecycle"], lifecycle)
                self.assertEqual(item["resolution_evidence"]["reason_code"], "terminal_task_no_remaining_work")
                self.assertEqual(self.run_check(record=True)["changes"], [])

    def merged_review(self):
        self.conn.execute(
            "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,state,metadata_json) "
            "VALUES('source','repo','github:example/repo!1','pull_request',1,'closed',?)",
            (json.dumps({"remote_state": {"merged": True, "merged_at": NOW}}),),
        )
        self.conn.execute("UPDATE workstreams SET lifecycle='completed',primary_source_id='source'")
        self.conn.execute("UPDATE tasks SET lifecycle='failed',route_id='review-pr'")
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,finished_at,metadata_json) "
            "VALUES('attempt','task',1,'completed',?,?,?)",
            (NOW, NOW, json.dumps({"process_stopped_at": NOW})),
        )
        self.conn.execute(
            "INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,"
            "created_at,updated_at,terminal_at) VALUES('task','task','human_request','terminal',?,?,?)",
            (NOW, NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) "
            "VALUES('result','task','attempt','multi_agent_review_report',?,?)",
            (NOW, json.dumps({"audit": {"status": "accepted"}})),
        )
        self.conn.execute(
            "INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,"
            "created_at,metadata_json) VALUES('action','result','task','submit_pr_review','accepted','skipped',?,?)",
            (NOW, json.dumps({"publish": {"reason_code": "superseded", "skipped_at": NOW}})),
        )
        self.conn.commit()

    def test_merged_superseded_review_is_dismissed_without_rewriting_failure(self):
        self.merged_review()
        result = self.run_check(record=True)
        self.assertEqual([c["transition"] for c in result["changes"]], ["dismissed"])
        self.assertEqual(self.active(), [])
        self.assertEqual(self.conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed")
        self.assertEqual(self.run_check(record=True)["changes"], [])

    def test_merged_review_requires_proven_merge_and_settled_execution_and_publication(self):
        self.merged_review()
        statements = (
            "UPDATE github_sources SET state='open'",
            "UPDATE github_sources SET metadata_json='{}'",
            "UPDATE workstreams SET lifecycle='active'",
            "UPDATE tasks SET route_id='update-existing-pr'",
            "UPDATE attempts SET metadata_json='{}'",
            "UPDATE execution_subjects SET binding_status='attention'",
            "UPDATE github_actions SET publish_status='not_published'",
            "UPDATE github_actions SET metadata_json='{}'",
            "UPDATE worker_results SET metadata_json='{}'",
            "UPDATE worker_results SET metadata_json='{\"audit\":[]}'",
            "UPDATE tasks SET updated_at='2099-01-01T00:00:00+00:00'",
            'UPDATE github_sources SET metadata_json=\'{"remote_state":{"merged":false}}\'',
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.conn.execute("SAVEPOINT refusal")
                self.conn.execute(statement)
                # Query the proof in the same writer so refusal cannot depend on sampling.
                self.assertEqual(store.dismiss_terminal_tasks(self.conn, ["task"], NOW), [])
                self.conn.execute("ROLLBACK TO refusal")
                self.conn.execute("RELEASE refusal")
        self.conn.commit()
        self.assertEqual(len(self.active()), 1)

    def test_live_or_successful_task_is_not_dismissed(self):
        for lifecycle in ("queued", "running", "completed", "failed"):
            with self.subTest(lifecycle=lifecycle):
                self.conn.execute("UPDATE tasks SET lifecycle=?", (lifecycle,))
                self.conn.commit()
                self.run_check(record=True)
                self.assertEqual(len(self.active()), 1)

    def test_terminal_owner_and_same_lineage_are_required(self):
        for statement in (
            "UPDATE workstreams SET lifecycle='active'",
            "UPDATE tasks SET metadata_json='{}'",
            'UPDATE tasks SET metadata_json=\'{"execution_attention":true,"supervision_lineage_key":"lineage"}\'',
        ):
            with self.subTest(statement=statement):
                self.conn.execute(statement)
                self.conn.commit()
                self.run_check(record=True)
                self.assertEqual(len(self.active()), 1)
                self.conn.execute("UPDATE workstreams SET lifecycle='canceled'")
                self.conn.execute('UPDATE tasks SET metadata_json=\'{"supervision_lineage_key":"lineage"}\'')
                self.conn.commit()

    def test_unknown_process_or_active_subject_blocks_retirement(self):
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES('attempt','task',1,'canceled','{\"dispatch\":{\"pid\":123}}')"
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute(
            'UPDATE attempts SET metadata_json=\'{"dispatch":{"pid":123},"process_stopped_at":"2026-11-21T09:00:00+00:00"}\''
        )
        self.conn.execute(
            "INSERT INTO execution_subjects(subject_type,subject_id,workflow_kind,binding_status,created_at,updated_at) VALUES('task','task','human_request','attention',?,?)",
            (NOW, NOW),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE execution_subjects SET binding_status='terminal',terminal_at=?", (NOW,))
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(self.active(), [])

    def test_unresolved_and_unknown_publication_blocks_even_skipped_action(self):
        self.conn.execute(
            "INSERT INTO github_actions(action_id,task_id,action_type,audit_status,publish_status,created_at) VALUES('action','task','comment','accepted','skipped',?)",
            (NOW,),
        )
        self.conn.execute(
            "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at) VALUES('intent','action','key','fingerprint','executing',?,?)",
            (NOW, NOW),
        )
        self.conn.commit()
        for status in ("executing", "attention", "failed", "prepared", "published"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE publication_intents SET status=?", (status,))
                self.conn.commit()
                self.run_check(record=True)
                self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE publication_intents SET status='confirmed_absent'")
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(self.active(), [])

    def test_malformed_effect_evidence_preserves_attention_without_failing_supervision(self):
        for payload in ("not-json", "[]", '{"publish":[]}'):
            with self.subTest(payload=payload):
                self.conn.execute(
                    "INSERT OR REPLACE INTO github_actions(action_id,task_id,action_type,audit_status,"
                    "publish_status,created_at,metadata_json) VALUES('action','task','comment','accepted','skipped',?,?)",
                    (NOW, payload),
                )
                self.conn.commit()
                self.assertEqual(self.run_check(record=True)["changes"], [])
                self.assertEqual(len(self.active()), 1)

    def test_writer_revalidation_and_transaction_rollback_then_retry(self):
        original = checks.supervision_observe.collect

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE workstreams SET lifecycle='active'")
            return result

        with patch.object(checks.supervision_observe, "collect", side_effect=changed):
            self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE workstreams SET lifecycle='canceled'")
        self.conn.commit()
        with patch.object(checks.supervision_actions, "plan", side_effect=ValueError("rollback")):
            self.assertFalse(checks.check(self.config, record=True, now=NOW)["ok"])
        self.assertEqual(len(self.active()), 1)
        self.run_check(record=True)
        self.assertEqual(self.active(), [])

    def test_recovery_and_reserved_native_slot_preserve_attention(self):
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','task',1,'canceled')"
        )
        self.conn.execute(
            "INSERT INTO supervision_recovery_actions(action_id,lineage_key,scope_type,scope_id,stage_family,action_kind,recovery_ordinal,idempotency_key,target_id,target_generation,policy_version,status,created_at) VALUES('recovery','lineage','task','task','execution','spool_replay',1,'key','attempt','gen','v1','unknown',?)",
            (NOW,),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE supervision_recovery_actions SET status='failed'")
        self.conn.execute(
            "INSERT INTO pre_pr_review_sessions(round_id,task_id,request_json,created_at,updated_at) VALUES('round','task','{}',?,?)",
            (NOW, NOW),
        )
        self.conn.execute(
            "INSERT INTO pre_pr_review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,output_dir,prompt_sha256,slot_reserved,created_at,updated_at) VALUES('run','round','review','worker',1,'attempt','succeeded','/out','sha',1,?,?)",
            (NOW, NOW),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE pre_pr_review_runs SET slot_reserved=0,dispatch_json='{\"pid\":123}'")
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute('UPDATE pre_pr_review_runs SET dispatch_json=\'{"pid":123,"process_exited":true}\'')
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(self.active(), [])

    def test_published_action_requires_same_result_verified_receipt(self):
        scope = {"repo": "example/repo"}
        receipt = {
            "schema_version": 1,
            "scope": scope,
            "result_id": "result",
            "action_id": "action",
            "task_id": "task",
            "action_type": "comment",
            "external_id": "12",
            "target_url": "https://github.com/example/repo/issues/1#issuecomment-12",
            "confirmed_at": NOW,
            "confirmation_method": "publisher_acknowledgement",
        }
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','task',1,'canceled')"
        )
        self.conn.execute(
            "INSERT INTO worker_results(result_id,task_id,attempt_id,output_type,created_at,metadata_json) VALUES('result','task','attempt','comment_analysis',?,?)",
            (NOW, json.dumps({"audit": {"status": "accepted", "action_scope": scope}})),
        )
        payload = {"type": "comment", "publish": {"receipt": receipt}}
        self.conn.execute(
            "INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,external_id,target_url,created_at,metadata_json) VALUES('action','result','task','comment','accepted','published','12',?,?,?)",
            (receipt["target_url"], NOW, json.dumps(payload)),
        )
        self.conn.execute(
            "INSERT INTO publication_intents(publication_id,action_id,idempotency_key,request_fingerprint,status,created_at,updated_at) VALUES('intent','action','key','fingerprint','published',?,?)",
            (NOW, NOW),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute(
            "UPDATE publication_intents SET remote_evidence_json=?",
            (json.dumps({"receipt": {**receipt, "result_id": "wrong"}}),),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE publication_intents SET remote_evidence_json=?", (json.dumps({"receipt": receipt}),))
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(self.active(), [])

    def test_unknown_lease_and_other_live_lineage_owner_block(self):
        self.conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES('attempt','task',1,'canceled')"
        )
        self.conn.execute(
            "INSERT INTO leases(lease_id,resource_type,resource_key,owner_id,status,acquired_at,expires_at,heartbeat_at) VALUES('lease','worker','attempt','attempt','expired',?,?,?)",
            (NOW, NOW, NOW),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE leases SET status='released'")
        self.conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at,metadata_json) VALUES('child','stream','queued',?,?,'{\"supervision_lineage_key\":\"lineage\"}')",
            (NOW, NOW),
        )
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(len(self.active()), 1)
        self.conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id='child'")
        self.conn.commit()
        self.run_check(record=True)
        self.assertEqual(self.active(), [])

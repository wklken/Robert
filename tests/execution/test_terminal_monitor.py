"""Stopped workflow authority must not strand exited worker attempts."""

from contextlib import closing
from datetime import datetime, timezone
import json
from unittest.mock import Mock, patch

from tests.support import database as fixture_database
from tests.support import terminal_worker
from tests.support.terminal_worker import TerminalWorkerFixture
from robert_agent.adapters.processes import attempt as attempt_process
from robert_agent.delivery.application import result_audit, submission
from robert_agent.delivery.lock import task_publication_lock
from robert_agent.execution import capacity, ownership, rounds, spool
from robert_agent.execution.application import evidence, monitor, readiness
from robert_agent.persistence import database, execution_control
from robert_agent.persistence.rounds import pre_pr_review
from robert_agent.persistence.schema import database as schema


class TerminalMonitorTests(TerminalWorkerFixture):
    def setUp(self):
        super().setUp()
        self.db = self.root / "robert.sqlite3"
        schema.init_database(self.db)
        self.now = datetime.now(timezone.utc).isoformat()
        self.config = {"hard_timeout_seconds": 3600, "heartbeat_stale_seconds": 1200}
        self.start_worker()
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES('repo','x/y','bot','main','/repo','/trees')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,lifecycle,active_task_id,created_at,updated_at) "
                "VALUES('stream','repo','active','task',?,?)",
                (self.now, self.now),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,metadata_json,created_at,updated_at) "
                "VALUES('task','stream','merge_conflict_remediation','running',?,?,?)",
                (json.dumps({"remediation_episode_id": "episode"}), self.now, self.now),
            )
            conn.execute(
                "INSERT INTO pr_remediation_episodes(episode_id,workstream_id,episode_kind,subject_key,"
                "observed_head_sha,observed_base_sha,status,first_seen_at,updated_at,last_task_id) "
                "VALUES('episode','stream','merge_conflict','head:base','head','base','remediating',?,?,'task')",
                (self.now, self.now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,heartbeat_at,metadata_json) "
                "VALUES('attempt','task',1,'running',?,?,?)",
                (self.now, self.now, json.dumps({"dispatch": self.dispatch, "result_contract": "compact-v1"})),
            )
            conn.execute(
                "INSERT INTO route_decisions(route_decision_id,task_id,route_id,confidence,"
                "allowed_github_actions_json,expected_output,created_at) "
                "VALUES('route','task','update-existing-pr','high','[]','update_existing_pr',?)",
                (self.now,),
            )
            self.assertTrue(ownership.claim_task(conn, "task", self.now))
            conn.execute("UPDATE pr_remediation_episodes SET status='superseded' WHERE episode_id='episode'")
            execution_control.close_binding(conn, "remediation_episode", "episode", self.now)

    def supervise(self, *, allow_recovery=True, task_lock_held=False):
        resume = Mock(side_effect=AssertionError("terminal authority cannot resume"))
        with (
            patch.object(
                evidence, "inspect_attempt_recovery_with_metadata", side_effect=AssertionError("no Git recovery")
            ),
            closing(database.connect(self.db)) as conn,
        ):
            count = monitor.supervise_running_attempts(
                conn,
                "repo",
                self.config,
                self.now,
                data_dir=self.root,
                db_path=self.db,
                resume_attempt=resume,
                allow_recovery=allow_recovery,
                **({"task_lock_held": True, "task_id": "task"} if task_lock_held else {}),
            )
            conn.commit()
        resume.assert_not_called()
        return count

    def test_exited_superseded_attempt_finishes_in_both_monitor_modes(self):
        self.stop_worker()
        for allow_recovery in (True, False):
            with self.subTest(allow_recovery=allow_recovery):
                with fixture_database.connect(self.db) as conn:
                    conn.execute("UPDATE attempts SET status='running',finished_at=NULL WHERE attempt_id='attempt'")
                    conn.execute("UPDATE tasks SET lifecycle='running' WHERE task_id='task'")
                    conn.execute(
                        "UPDATE workstreams SET lifecycle='active',active_task_id='task' WHERE workstream_id='stream'"
                    )
                self.assertEqual(self.supervise(allow_recovery=allow_recovery), 1)
                with fixture_database.connect(self.db) as conn:
                    attempt = conn.execute("SELECT status,finished_at,failure_json FROM attempts").fetchone()
                    self.assertEqual(attempt["status"], "failed")
                    self.assertIsNotNone(attempt["finished_at"])
                    self.assertEqual(json.loads(attempt["failure_json"])["status"], "execution_authority_ended")
                    self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed")
                    self.assertIsNone(conn.execute("SELECT active_task_id FROM workstreams").fetchone()[0])
                    self.assertEqual(capacity.running_attempt_count(conn, "repo"), 0)
                    self.assertEqual(
                        execution_control.get_binding(conn, "remediation_episode", "episode")["binding_status"],
                        "terminal",
                    )
                    self.assertFalse(ownership.claim_task(conn, "task", self.now))
                    self.assertFalse(
                        readiness.has_local_work(
                            conn, ["repo"], config_result=self.config, now=self.now, include_publication=False
                        )
                    )
                self.assertEqual(self.supervise(allow_recovery=allow_recovery), 0)

    def test_live_worker_keeps_attempt_until_exit_is_proven(self):
        self.assertEqual(self.supervise(), 0)
        self.assertIsNone(self.worker.poll())
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")
            self.assertEqual(capacity.running_attempt_count(conn, "repo"), 1)

    def test_publication_lock_defers_cleanup_and_supports_paused_caller(self):
        self.stop_worker()
        with task_publication_lock(self.db, "task") as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.supervise(), 0)
            self.assertEqual(self.supervise(allow_recovery=False, task_lock_held=True), 1)

    def test_unknown_native_child_keeps_parent_capacity(self):
        self.stop_worker()
        with patch.object(rounds, "observe_exit", return_value={"all_stopped": False}):
            self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")

    def test_unknown_worker_identity_keeps_capacity_without_signaling(self):
        with (
            patch.object(attempt_process, "dispatch_status", return_value={"status": "unknown"}),
            patch.object(attempt_process, "terminate_attempt", side_effect=AssertionError("unknown process")),
        ):
            self.assertEqual(self.supervise(), 0)
        self.assertIsNone(self.worker.poll())

    def test_real_exited_model_child_releases_its_reserved_slot(self):
        with fixture_database.connect(self.db) as conn:
            pre_pr_review.create(conn, "task", {})
            session = pre_pr_review.session(conn, task_id="task")
            run = pre_pr_review.new_run(conn, session["round_id"], "implement", "coder", "attempt", self.root, "hash")
            pre_pr_review.update_run(
                conn, run["run_id"], status="running", slot_reserved=1, dispatch_json=json.dumps(self.dispatch)
            )
        self.stop_worker()
        self.assertEqual(self.supervise(), 1)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT slot_reserved FROM pre_pr_review_runs").fetchone()[0], 0)
            self.assertEqual(capacity.running_attempt_count(conn, "repo"), 0)

    def test_changed_attempt_snapshot_defers_cleanup(self):
        self.stop_worker()
        observe = rounds.observe_exit

        def advance(conn, task_id):
            observation = observe(conn, task_id)
            with fixture_database.connect(self.db) as writer:
                writer.execute("UPDATE attempts SET heartbeat_at='2099-01-01T00:00:00Z' WHERE attempt_id='attempt'")
            return observation

        with patch.object(rounds, "observe_exit", side_effect=advance):
            self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")

    def test_attention_binding_is_not_terminal_cleanup_authority(self):
        self.stop_worker()
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE execution_subjects SET binding_status='attention'")
        self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")

    def test_cleanup_preserves_successor_task_and_workstream(self):
        self.stop_worker()
        observe = rounds.observe_exit

        def advance(conn, task_id):
            result = observe(conn, task_id)
            with fixture_database.connect(self.db) as writer:
                writer.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id='task'")
                writer.execute(
                    "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES('successor','stream','queued',?,?)",
                    (self.now, self.now),
                )
                writer.execute("UPDATE workstreams SET active_task_id='successor' WHERE workstream_id='stream'")
            return result

        with patch.object(rounds, "observe_exit", side_effect=advance):
            self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT lifecycle,active_task_id FROM workstreams").fetchone()),
                ("active", "successor"),
            )
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks WHERE task_id='successor'").fetchone()[0], "queued"
            )

    def test_saved_result_defers_cleanup_for_reconciliation(self):
        self.stop_worker()
        with spool.locked_record(self.db, "attempt") as path:
            path.write_text("{}")
        self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT status FROM attempts").fetchone()[0], "running")

    def submit_before_episode_closes(self):
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE execution_subjects SET binding_status='active',terminal_at=NULL")
            conn.execute("UPDATE pr_remediation_episodes SET status='remediating'")
            conn.execute("UPDATE route_decisions SET route_id='local-result',expected_output='local_result'")
        response = submission.record_result(
            self.db,
            submission.build_result(
                "task",
                "attempt",
                "local_result",
                ["comment:1"],
                [],
                "Saved repair evidence",
                remediation_evidence={"resolution_summary": "Repair completed"},
            ),
        )
        self.assertEqual(response["status"], "recorded", response)
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE pr_remediation_episodes SET status='superseded'")
            execution_control.close_binding(conn, "remediation_episode", "episode", self.now)
            conn.execute(
                "INSERT INTO github_actions(action_id,result_id,task_id,action_type,audit_status,publish_status,created_at,metadata_json) "
                "VALUES('action',?,'task','push_existing_pr','pending','not_published',?,'{}')",
                (response["result_id"], self.now),
            )
        return response["result_id"]

    def test_received_result_is_retained_and_pending_actions_retired_after_exit(self):
        self.submit_before_episode_closes()
        self.assertEqual(self.supervise(), 0)
        self.stop_worker()
        self.assertEqual(self.supervise(), 1)
        with fixture_database.connect(self.db) as conn:
            result = conn.execute("SELECT handoff,metadata_json FROM worker_results").fetchone()
            self.assertEqual(result["handoff"], "Saved repair evidence")
            metadata = json.loads(result["metadata_json"])
            self.assertIn("submission_fingerprint", metadata)
            self.assertEqual(metadata["terminal_reconciliation"]["status"], "execution_authority_ended")
            self.assertEqual(conn.execute("SELECT publish_status FROM github_actions").fetchone()[0], "skipped")
            self.assertIsNone(conn.execute("SELECT active_task_id FROM workstreams").fetchone()[0])
            self.assertEqual(capacity.running_attempt_count(conn, "repo"), 0)
            self.assertFalse(ownership.claim_task(conn, "task", self.now))
        self.assertEqual(self.supervise(), 0)

    def test_received_result_with_unknown_publication_keeps_capacity_until_reconciled(self):
        self.submit_before_episode_closes()
        with fixture_database.connect(self.db) as conn:
            intent = execution_control.prepare_publication_intent(
                conn, "action", "publication-key", "request-key", self.now
            )
            execution_control.transition_publication_intent(conn, intent["publication_id"], "executing", self.now)
        self.stop_worker()
        self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(capacity.running_attempt_count(conn, "repo"), 1)
            self.assertEqual(conn.execute("SELECT publish_status FROM github_actions").fetchone()[0], "not_published")
            execution_control.transition_publication_intent(
                conn,
                intent["publication_id"],
                "confirmed_absent",
                self.now,
                remote_evidence={"status": "confirmed_absent"},
            )
        self.assertEqual(self.supervise(allow_recovery=False), 1)

    def test_already_observed_exit_does_not_hide_unreconciled_result(self):
        self.submit_before_episode_closes()
        self.stop_worker()
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "UPDATE attempts SET status='completed',metadata_json=json_set(metadata_json,'$.process_stopped_at',?)",
                (self.now,),
            )
        with closing(database.connect(self.db)) as conn:
            audited = result_audit.audit_completed_results(
                conn,
                "repo",
                self.root,
                self.db,
                {},
                "run",
                self.now,
                False,
                [],
                {"remaining": 1},
            )
            conn.commit()
        self.assertEqual(audited, 1)
        with fixture_database.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT active_task_id FROM workstreams").fetchone()[0])

    def test_terminal_reconciliation_preserves_audit_and_published_receipt(self):
        self.submit_before_episode_closes()
        self.stop_worker()
        audit = {
            "status": "accepted",
            "audited_at": self.now,
            "action_scope": {"task_kind": "merge_conflict_remediation"},
        }
        receipt = {"confirmation_method": "remote_reconciliation", "confirmed_at": self.now}
        with fixture_database.connect(self.db) as conn:
            conn.execute(
                "UPDATE worker_results SET metadata_json=json_set(metadata_json,'$.audit',json(?))",
                (json.dumps(audit),),
            )
            conn.execute(
                "UPDATE github_actions SET audit_status='accepted',publish_status='published',metadata_json=?",
                (json.dumps({"publish": {"receipt": receipt}}),),
            )
        self.assertEqual(self.supervise(), 1)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["audit"], audit
            )
            action = conn.execute("SELECT publish_status,metadata_json FROM github_actions").fetchone()
            self.assertEqual(action[0], "published")
            self.assertEqual(json.loads(action[1])["publish"]["receipt"], receipt)

    def test_unattached_published_intent_defers_result_cleanup(self):
        self.submit_before_episode_closes()
        self.stop_worker()
        with fixture_database.connect(self.db) as conn:
            intent = execution_control.prepare_publication_intent(
                conn, "action", "publication-key", "request-key", self.now
            )
            execution_control.transition_publication_intent(conn, intent["publication_id"], "executing", self.now)
            execution_control.transition_publication_intent(conn, intent["publication_id"], "published", self.now)
        self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(capacity.running_attempt_count(conn, "repo"), 1)

    def test_received_result_with_unknown_process_identity_is_not_cleaned_up(self):
        self.submit_before_episode_closes()
        with patch.object(attempt_process, "dispatch_status", return_value={"status": "unknown"}):
            self.assertEqual(self.supervise(), 0)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(capacity.running_attempt_count(conn, "repo"), 1)
            self.assertEqual(conn.execute("SELECT publish_status FROM github_actions").fetchone()[0], "not_published")

    def test_terminal_authority_still_rejects_worker_submission(self):
        response = submission.record_result(
            self.db,
            {
                "task_id": "task",
                "attempt_id": "attempt",
                "output_type": "comment_analysis",
                "submission": {
                    "planned_github_actions": [],
                    "consumed_event_fingerprints": [],
                    "verification": [],
                    "handoff": "stopped",
                    "used_skills": [],
                },
            },
        )
        self.assertEqual(response["status"], "rejected_by_workflow_owner")


class TerminalWorkerStartupTests(TerminalWorkerFixture):
    def test_delayed_child_is_ready_before_identity_capture(self):
        ready = self.root / "worker.ready"
        popen = terminal_worker.Popen
        capture = terminal_worker.identity.capture_process_identity

        def delayed_child(command, **kwargs):
            command = [*command[:2], "import time; time.sleep(0.05); " + command[2]]
            return popen(command, **kwargs)

        def capture_ready_worker(pid):
            self.assertTrue(ready.exists(), "identity captured before worker was ready")
            return capture(pid, proc_root=self.root / "missing-proc")

        with (
            patch.object(terminal_worker, "Popen", side_effect=delayed_child),
            patch.object(terminal_worker.identity, "capture_process_identity", side_effect=capture_ready_worker),
        ):
            self.start_worker()
        self.assertEqual(self.dispatch["identity_source"], terminal_worker.identity.IDENTITY_SOURCE_PS)
        self.assertEqual(terminal_worker.identity.inspect_recorded_identity(self.dispatch), "alive_same")

    def test_child_exit_before_readiness_fails_without_capturing_identity(self):
        popen = terminal_worker.Popen

        def exiting_child(command, **kwargs):
            return popen([*command[:2], "raise SystemExit(0)"], **kwargs)

        with (
            patch.object(terminal_worker, "Popen", side_effect=exiting_child),
            patch.object(terminal_worker.identity, "capture_process_identity") as capture,
        ):
            with self.assertRaisesRegex(AssertionError, "worker did not become ready"):
                self.start_worker()
        capture.assert_not_called()
        self.doCleanups()
        self.assertEqual(self.worker.poll(), 0)

    def test_readiness_timeout_fails_and_cleans_up_live_child(self):
        popen = terminal_worker.Popen

        def unready_child(command, **kwargs):
            return popen([*command[:2], "import time; time.sleep(120)"], **kwargs)

        with (
            patch.object(terminal_worker, "Popen", side_effect=unready_child),
            patch.object(terminal_worker.time, "monotonic", side_effect=[0, 0, 5]),
            patch.object(terminal_worker.time, "sleep"),
            patch.object(terminal_worker.identity, "capture_process_identity") as capture,
        ):
            with self.assertRaisesRegex(AssertionError, "worker did not become ready"):
                self.start_worker()
        capture.assert_not_called()
        self.doCleanups()
        self.assertIsNotNone(self.worker.poll())

from contextlib import closing
import json
import sqlite3

from robert_agent.delivery.application import submission
from tests.support.worker_protocol import WorkerProtocolFixture


class NativeResultContractTests(WorkerProtocolFixture):
    def _answer(self):
        return {
            "task_id": "task-1",
            "attempt_id": "attempt-1",
            "output_type": "comment_analysis",
            "submission": {
                "planned_actions": [{"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "处理说明"}],
                "consumed_event_fingerprints": ["comment:1"],
                "used_skills": [],
                "verification": [],
                "handoff": "Answer saved",
            },
        }

    def _native_attempt(
        self,
        *,
        role="answer",
        output="comment_analysis",
        route="comment-analysis",
        capability="readonly",
        allowed_actions=None,
    ):
        db = self._init_attempt_db(output_type=output, route_id=route)
        scope = {
            "provider": "tapd",
            "host": "tapd.example",
            "workspace_id": "42",
            "kind": "story",
            "external_id": "1",
            "action": "comment",
        }
        metadata = {
            "result_contract": "native-v1",
            "native_result": {
                "round_id": "round-1",
                "request_id": "request-1",
                "repo_id": "repo-1",
                "capability": capability,
                "role": role,
                "allowed_actions": allowed_actions
                if allowed_actions is not None
                else (
                    []
                    if role in ("evidence", "classification")
                    else [{"provider": "tapd", "type": "comment", "target_id": "ticket"}]
                ),
            },
        }
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "INSERT INTO sources(source_id, source_key, source_type, provider, host, project_key, external_id) VALUES ('tapd-1','tapd:story:1','story','tapd','tapd.example','42','1')"
            )
            conn.execute("UPDATE workstreams SET primary_source_id='tapd-1'")
            conn.execute("UPDATE tasks SET route_id=?", (route,))
            conn.execute(
                "INSERT INTO ingress_requests(request_id,source_id,provider,auth_type,authorization_json,content_snapshot_json,content_version,created_at,updated_at) VALUES ('request-1','tapd-1','tapd','mention','{}',?,'v1','now','now')",
                ('{"event_id":"event-1"}',),
            )
            conn.execute(
                "INSERT INTO source_sessions(source_id,owner,token,lease_until,updated_at) VALUES ('tapd-1','test',1,'2100-01-01T00:00:00+00:00','now')"
            )
            conn.execute(
                "INSERT INTO source_rounds(round_id,source_id,status,capability,repo_ids_json,publication_scope_json,input_version,input_snapshot_json,input_sha256,created_at) VALUES ('round-1','tapd-1','active',?,'[\"repo-1\"]',?,'v1','{}','digest','now')",
                (capability, json.dumps(scope)),
            )
            conn.execute(
                "INSERT INTO source_events(event_id,source_id,event_fingerprint,event_type,event_at) VALUES ('event-1','tapd-1','comment:1','comment','now')"
            )
            conn.execute(
                "INSERT INTO request_events(request_id,event_id,state,created_at) VALUES ('request-1','event-1','pending','now')"
            )
            conn.execute(
                "INSERT INTO source_round_requests VALUES ('round-1','request-1','v1',?)",
                ('{"event_id":"event-1"}',),
            )
            conn.execute("INSERT INTO task_events VALUES ('task-1','event-1','trigger','now')")
            conn.execute(
                "INSERT INTO source_round_tasks VALUES ('task-1','round-1','request-1','repo-1',?,'now')", (capability,)
            )
            conn.execute("UPDATE attempts SET metadata_json=?", (json.dumps(metadata),))
        return db

    def test_native_answer_is_bound_and_recorded(self):
        db = self._native_attempt()
        receipt = submission.record_result(db, self._answer())
        self.assertEqual(receipt.get("status"), "recorded", receipt)

    def test_canceled_native_pending_result_is_archived_without_consumption(self):
        from unittest.mock import patch
        from robert_agent.execution import spool

        db = self._native_attempt()
        envelope = self._answer()
        with patch.object(submission, "_record_result_db", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(submission.record_result(db, envelope)["status"], "deferred")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id='task-1'")
        self.assertEqual(submission.record_result(db, envelope)["status"], "rejected_by_supervisor")
        self.assertEqual(list(spool.directory(db).glob("*.json")), [])
        archives = list((spool.directory(db) / "rejected").glob("*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(json.loads(archives[0].read_text())["reason"], "rejected_by_supervisor")
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT state FROM request_events").fetchall(), [("pending",)])

    def _audit(self, db):
        from datetime import datetime, timezone
        from robert_agent.delivery.application import result_audit
        from robert_agent.persistence import database

        with closing(database.connect(db)) as conn, conn:
            return result_audit.audit_completed_results(
                conn,
                "repo-1",
                self.root,
                db,
                {"full_name": "x/y"},
                "run-1",
                datetime.now(timezone.utc).isoformat(),
                False,
                [],
                0,
            )

    def test_deferred_answer_replays_audits_and_stays_out_of_github_publisher(self):
        from unittest.mock import patch
        from robert_agent.delivery.application import publish
        from robert_agent.execution import spool
        from robert_agent.execution.application import cutover

        db = self._native_attempt()
        with patch.object(submission, "_record_result_db", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(submission.record_result(db, self._answer())["status"], "deferred")
        slot = next(spool.directory(db).glob("*.json"))
        pending = json.loads(slot.read_text())
        self.assertEqual((pending["result_contract"], pending["normalizer_version"]), ("native-v1", 1))
        self.assertTrue(cutover.check(db)["ok"])
        self.assertEqual(submission.replay_pending_results(db)["recorded"], 1)
        self.assertTrue(submission.record_result(db, self._answer())["deduplicated"])
        self.assertEqual(self._audit(db), 1)
        with closing(sqlite3.connect(db)) as conn:
            provider, target, legacy_url, audit, status = conn.execute(
                "SELECT provider,target_json,target_url,audit_status,publish_status FROM github_actions"
            ).fetchone()
            metadata = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])
            self.assertEqual((provider, legacy_url, audit, status), ("tapd", None, "accepted", "not_published"))
            self.assertEqual(json.loads(target)["workspace_id"], "42")
            self.assertNotIn("planned_actions", metadata["normalized_payload"])
            self.assertEqual(conn.execute("SELECT state FROM request_events").fetchall(), [("pending",)])

        def unexpected(*args, **kwargs):
            self.fail("native candidate must not invoke GitHub")

        self.assertEqual(publish.publish_ready_actions(db, run_command=unexpected)["published_count"], 0)

    def test_forged_target_provider_repo_version_and_mixed_fields_are_refused(self):
        db = self._native_attempt()
        for name, value in (("provider", "github"), ("target_id", "unbound"), ("repo_id", "other"), ("target", {})):
            with self.subTest(name=name):
                envelope = self._answer()
                envelope["submission"]["planned_actions"][0][name] = value
                self.assertEqual(submission.record_result(db, envelope)["status"], "failed_validation")
        for name, value in (("planned_github_actions", []), ("result_contract", "compact-v1")):
            envelope = self._answer()
            envelope["submission"][name] = value
            self.assertEqual(submission.record_result(db, envelope)["reason_code"], "program_owned_field")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.native_result.repo_id','other')")
        self.assertEqual(submission.record_result(db, self._answer())["status"], "unsupported_contract")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.result_contract','compact-v1')")
        self.assertEqual(submission.record_result(db, self._answer())["status"], "unsupported_contract")

    def test_readonly_child_needs_bound_evidence_and_group_exit(self):
        db = self._native_attempt(role="evidence", output="local_result", route="source-evidence")
        envelope = self._answer()
        envelope["output_type"] = "local_result"
        payload = envelope["submission"]
        payload["planned_actions"] = []
        ref = {"repo_id": "repo-1", "path": "src/file.py", "revision": "sha256:" + "a" * 64}
        payload["evidence"] = [{**ref, "repo_id": "other"}]
        self.assertEqual(submission.record_result(db, envelope)["field_path"], "evidence")
        payload["evidence"] = [{**ref, "path": "../private"}]
        self.assertEqual(submission.record_result(db, envelope)["field_path"], "evidence")
        payload["evidence"] = [ref]
        payload["planned_actions"] = [{"provider": "github", "type": "push_existing_pr", "target_id": "foreign"}]
        self.assertEqual(submission.record_result(db, envelope)["reason_code"], "action_scope")
        payload["planned_actions"] = []
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json,'$.dispatch',json_object('pid',123,'process_exited',json('true'),'cleanup_pending',json('true')))"
            )
        self.assertEqual(submission.record_result(db, envelope)["status"], "recorded")
        self.assertEqual(self._audit(db), 0)
        with closing(sqlite3.connect(db)) as conn, conn:
            from datetime import datetime, timezone
            from robert_agent.persistence.stores import attempts

            stopped_at = datetime.now(timezone.utc).isoformat()
            attempts.update_for_supervise_running_attempts_4(conn, stopped_at, stopped_at, "attempt-1")
        self.assertEqual(self._audit(db), 1)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "completed")
            self.assertEqual(conn.execute("SELECT status FROM source_rounds").fetchone()[0], "active")
            self.assertEqual(conn.execute("SELECT state FROM request_events").fetchall(), [("pending",)])
            self.assertEqual(conn.execute("SELECT count(*) FROM github_actions").fetchone()[0], 0)

    def test_readonly_classification_handoff_cannot_recommend_development(self):
        db = self._native_attempt(role="classification", output="classification_result", route="classification-result")
        envelope = self._answer()
        envelope["output_type"] = "classification_result"
        envelope["submission"].update(planned_actions=[], recommended_route="new-pr")
        self.assertEqual(submission.record_result(db, envelope)["field_path"], "recommended_route")
        envelope["submission"]["recommended_route"] = "comment-analysis"
        self.assertEqual(submission.record_result(db, envelope)["status"], "recorded")
        self.assertEqual(self._audit(db), 1)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "completed")
            self.assertEqual(conn.execute("SELECT state FROM request_events").fetchall(), [("pending",)])

    def test_spool_payload_contract_mismatch_never_ingests(self):
        from unittest.mock import patch
        from robert_agent.execution import spool
        from robert_agent.execution.application import cutover

        db = self._native_attempt()
        with patch.object(submission, "_record_result_db", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(submission.record_result(db, self._answer())["status"], "deferred")
        slot = next(spool.directory(db).glob("*.json"))
        pending = json.loads(slot.read_text())
        pending["payload"]["_result_contract"] = "compact-v1"
        spool.atomic_write(slot, pending)
        self.assertFalse(cutover.check(db)["ok"])
        self.assertEqual(submission.replay_pending_results(db)["recorded"], 0)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)

    def test_compact_attempt_and_frozen_prompt_survive_native_cutover(self):
        from unittest.mock import patch
        from robert_agent.execution import spool
        from robert_agent.execution.application import cutover

        db = self._init_attempt_db()
        prompt = self.root / "frozen-prompt.md"
        prompt.write_bytes(b"original compact-v1 prompt\r\n")
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET metadata_json=json_set(metadata_json,'$.prompt_path',?)", (str(prompt),))
        envelope = submission.build_result(
            "task-1",
            "attempt-1",
            "comment_analysis",
            ["comment:1"],
            [],
            "done",
            planned_github_actions=[{"type": "comment", "body": "Answer."}],
        )
        with patch.object(submission, "_record_result_db", side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(submission.record_result(db, envelope)["status"], "deferred")
        saved = json.loads(next(spool.directory(db).glob("*.json")).read_text())
        self.assertEqual((saved["result_contract"], saved["normalizer_version"]), ("compact-v1", 3))
        self.assertTrue(cutover.prepare(db)["ok"])
        self.assertEqual(submission.replay_pending_results(db)["recorded"], 1)
        self.assertEqual(self._audit(db), 1)
        self.assertEqual(prompt.read_bytes(), b"original compact-v1 prompt\r\n")
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(
                conn.execute("SELECT provider,target_json,audit_status FROM github_actions").fetchone(),
                ("github", None, "accepted"),
            )

    def test_development_selects_code_actions_without_worker_github_text(self):
        from unittest.mock import patch

        db = self._native_attempt(
            role="development",
            output="new_pr",
            route="new-pr",
            capability="development",
            allowed_actions=[
                {"provider": "github", "type": "push_existing_pr", "target_id": "repo"},
                {"provider": "github", "type": "open_pr", "target_id": "repo"},
                {"provider": "tapd", "type": "comment", "target_id": "ticket"},
            ],
        )
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE attempts SET branch_name='codex/fixed',worktree_path='/test/worktree'")
        envelope = self._answer()
        envelope["output_type"] = "new_pr"
        fields = envelope["submission"]
        code = [
            {"provider": "github", "type": "push_existing_pr", "target_id": "repo"},
            {"provider": "github", "type": "open_pr", "target_id": "repo"},
        ]
        fields["planned_actions"] = [*code, *fields["planned_actions"]]
        fields["verification"] = [
            {
                "source": "worker_reported",
                "command": "test",
                "status": "passed",
                "purpose": "Regression",
                "required": True,
                "exit_code": 0,
            }
        ]
        fields["planned_actions"][1]["body"] = "TAPD material in GitHub text"
        self.assertEqual(submission.record_result(db, envelope)["status"], "failed_validation")
        del fields["planned_actions"][1]["body"]
        with (
            patch("robert_agent.delivery.application.scope.observe_head", return_value="a" * 40),
            patch("robert_agent.delivery.application.scope.observe_worktree_status", return_value="clean"),
        ):
            self.assertEqual(submission.record_result(db, envelope)["status"], "recorded")
        with patch(
            "robert_agent.delivery.application.scope.for_result",
            return_value={
                "repo": "x/y",
                "worktree_path": "/test/worktree",
                "remote": "origin",
                "base_branch": "master",
                "branch_name": "codex/fixed",
                "result_head_sha": "a" * 40,
                "worktree_status": "clean",
            },
        ):
            self.assertEqual(self._audit(db), 1)
        from robert_agent.delivery.application import publish

        self.assertEqual(publish.publish_ready_actions(db, dry_run=True)["pending_count"], 0)
        with closing(sqlite3.connect(db)) as conn:
            audit = json.loads(conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0])["audit"]
            self.assertEqual(audit["violations"], ["unverified_native_git"])
            stored = conn.execute(
                "SELECT metadata_json,target_json,audit_status FROM github_actions WHERE provider='github'"
            ).fetchall()
            self.assertEqual(len(stored), 2)
            for metadata, target, status in stored:
                self.assertEqual(json.loads(metadata), {"target_id": "repo"})
                self.assertEqual(json.loads(target)["repository"], "x/y")
                self.assertEqual(status, "policy_violation")

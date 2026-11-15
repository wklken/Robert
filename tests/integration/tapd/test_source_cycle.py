"""Exercise the source coordinator with normalized config and real durable use cases."""

from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import yaml

from robert_agent.adapters.processes.source_model import SourceModelOutcome
from robert_agent.application.source_cycle import advance_source, has_due_source
from robert_agent.execution.application import dispatch, source_models
from robert_agent.foundation import values
from robert_agent.persistence import database, transactions
from tests.support.source_cycle import SourceCycleFixture


class SourceCycleTests(SourceCycleFixture):
    def test_development_classification_drives_real_answer_and_root(self):
        self.config(development=True)
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="comment-analysis")
        self.tick("waiting")
        self.tick("audited")
        self.tick("prepared")
        self.tick("dispatched")
        self.submit(
            "comment_analysis",
            planned_actions=[
                {"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Repository findings."},
            ],
        )
        self.tick("waiting")
        self.tick("audited")
        accepted = self.tick("publication_pending")
        self.assertEqual(self.tick("publication_pending", ticket=self.ticket).identity, accepted.identity)
        self.assertEqual(self.read("SELECT count(*) FROM tasks"), [(2,)])
        self.assertEqual(
            self.read("SELECT state,source_result_id FROM request_events"), [("consumed", accepted.identity)]
        )

    def test_readonly_identification_question_needs_no_task(self):
        self.config(two=True)
        self.model_kind = "question"
        self.tick("admitted", ticket=self.ticket)
        self.tick("stage_recorded")
        root = self.tick("publication_pending")
        self.assertEqual(
            self.read("SELECT kind FROM source_results WHERE source_result_id=?", (root.identity,)), [("question",)]
        )
        self.assertEqual(self.read("SELECT count(*) FROM tasks"), [(0,)])
        self.assertEqual(len(self.model_calls), 1)

    def submit_evidence(self):
        started = self.started[-1]
        [[repo_id]] = self.read("SELECT repo_id FROM source_round_tasks WHERE task_id=?", (started["task_id"],))
        [repo] = [repo for repo in self.configured["repos"] if values.repository_id(repo["full_name"]) == repo_id]
        digest = sha256((Path(repo["repo_root"]) / "README.md").read_bytes()).hexdigest()
        self.submit(
            "local_result", evidence=[{"repo_id": repo_id, "path": "README.md", "revision": "sha256:" + digest}]
        )

    def test_readonly_two_selected_repositories_run_serially_then_synthesize(self):
        self.config(two=True)
        admitted = self.tick("admitted", ticket=self.ticket)
        self.tick("stage_recorded")
        self.tick("prepared")
        self.tick("dispatched")
        self.submit_evidence()
        self.tick("waiting")
        self.tick("audited")
        self.comments = [self.comment("1", suffix=" ordinary edit"), self.comment("2"), self.comment("3")]
        self.tick("prepared")
        self.tick("dispatched")
        self.submit_evidence()
        self.tick("waiting")
        self.tick("audited")
        self.tick("stage_recorded")
        accepted = self.tick("publication_pending")
        self.assertEqual(accepted.round_id, admitted.round_id)
        self.assertEqual(len(self.model_calls), 2)
        self.assertEqual(len(set(self.model_calls)), 2)
        self.assertEqual(
            self.read("SELECT state FROM request_events ORDER BY state"), [("consumed",), ("pending",), ("pending",)]
        )
        self.assertEqual(self.read("SELECT count(*) FROM tasks"), [(2,)])

    def test_waiting_classifier_prepares_fixed_question_route(self):
        self.config(development=True)
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="waiting-for-user")
        self.tick("waiting")
        self.tick("audited")
        prepared = self.tick("prepared")
        self.assertEqual(
            self.read("SELECT route_id,expected_output FROM tasks WHERE task_id=?", (prepared.identity,)),
            [("waiting-for-user", "waiting_for_user")],
        )

    def test_development_classifier_prepares_native_code_scope(self):
        from robert_agent.delivery import native_contract, tapd_policy
        from robert_agent.delivery.application import native_preparation
        from robert_agent.execution import workspace

        self.config(development=True)
        self.comments = [self.comment("1", suffix=" TOP-SECRET-771 https://tapd.example/internal")]
        repo = Path(self.configured["repos"][0]["repo_root"])
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Repository Bot")
        self.git(repo, "config", "user.email", "repo-bot@example.com")
        self.git(repo, "remote", "add", "upstream", "https://github.com/org/alpha.git")
        self.git(repo, "remote", "add", "origin", "https://github.com/bot/alpha.git")
        self.git(repo, "add", "README.md")
        self.git(repo, "commit", "-m", "Prior existing commit")
        initial = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "update-ref", "refs/remotes/upstream/main", initial)
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="new-pr")
        self.tick("waiting")
        self.tick("audited")

        def prepare_local_checkout(repo, purpose, *, pr=None):
            # Exercise the real linked worktree without a network fetch.
            self.assertEqual(purpose["mode"], "new_branch")
            self.assertIsNone(pr)
            branch = f"codex/dd-{purpose['workspace_key']}-source-work"
            target = Path(repo["worktree_root"]) / "prepared"
            target.parent.mkdir(parents=True)
            self.git(repo["repo_root"], "worktree", "add", "-b", branch, str(target), initial)
            return {"ok": True, "worktree_path": str(target), "branch_name": branch}

        with patch.object(workspace, "prepare_source_worktree", side_effect=prepare_local_checkout):
            prepared = self.tick("prepared")
        [[attempt_id, tree, encoded]] = self.read(
            "SELECT attempt_id,worktree_path,metadata_json FROM attempts WHERE task_id=?", (prepared.identity,)
        )
        purpose = json.loads(encoded)["native_workspace"]
        self.assertEqual(purpose["git_pin"]["initial_head"], initial)
        self.assertEqual(purpose["git_pin"]["author"], "Repository Bot <repo-bot@example.com>")
        [[prompt]] = self.read("SELECT path FROM artifacts WHERE attempt_id=?", (attempt_id,))
        self.assertIn(tapd_policy.NATIVE_COMMIT_MESSAGE, Path(prompt).read_text())
        self.tick("dispatched")
        path = Path(tree) / "README.md"
        path.write_text("Corrected repository behavior")
        self.git(tree, "add", "README.md")
        self.git(tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        head = self.git(tree, "rev-parse", "HEAD")
        body = "Updated README.md"
        actions = native_contract.selectors_for("new_pr")
        actions[-1]["body"] = body
        self.submit(
            "new_pr",
            planned_actions=actions,
            verification=[
                {
                    "command": ["pytest"],
                    "source": "worker_reported",
                    "exit_code": 0,
                    "status": "passed",
                    "required": True,
                    "purpose": "TOP-SECRET-771 https://tapd.example/internal",
                }
            ],
        )
        self.tick("waiting")
        self.tick("audited")
        [[result_json]] = self.read("SELECT metadata_json FROM worker_results WHERE attempt_id=?", (attempt_id,))
        audit = json.loads(result_json)["audit"]
        self.assertEqual(audit["status"], "accepted", audit)
        proof = audit["action_scope"]["native_github"]
        self.assertEqual(proof["head_sha"], head)
        self.assertEqual(proof["changed_paths"], [["modified", "README.md"]])
        self.assertIn("Worker-reported", proof["body"])
        for text in ("TOP-SECRET-771", "tapd.example", "requester", self.ticket.external_id):
            self.assertNotIn(text, proof["title"] + proof["body"] + proof["commit_message"])
        self.assertEqual(audit["native_tapd"]["mapped_paths"], ["README.md"])
        rows = self.read(
            "SELECT provider,metadata_json FROM github_actions WHERE task_id=? ORDER BY provider", (prepared.identity,)
        )
        self.assertEqual(
            [json.loads(metadata) for provider, metadata in rows if provider == "github"],
            [{"target_id": "repository"}, {"target_id": "repository"}],
        )
        self.assertEqual(json.loads(rows[-1][1]), {"target_id": "ticket", "body": body})
        with closing(database.connect(self.db)) as conn:
            from robert_agent.persistence.queries import source_results

            [[result_id]] = self.read("SELECT result_id FROM worker_results WHERE attempt_id=?", (attempt_id,))
            candidates = source_results.candidate_actions(conn, result_id)
        github_action = dict(next(action for action in candidates if action["action_type"] == "open_pr"))
        tapd_action = dict(next(action for action in candidates if action["provider"] == "tapd"))
        for action in (github_action, tapd_action):
            action["metadata_json"] = json.dumps(
                {**json.loads(action["metadata_json"]), "publish": {"status": "publish_failed", "retryable": True}}
            )
        self.assertEqual(native_preparation.worker_tapd_body(tapd_action, result_json), (body, ("README.md",)))
        effect = native_preparation.github_effect_metadata(github_action, audit["action_scope"])
        self.assertEqual(effect["body"], proof["body"])
        self.assertEqual(effect["head_sha"], head)
        self.assertTrue(native_preparation.recheck_github(audit["action_scope"]))
        poisoned = {**audit["action_scope"], "native_github": {**proof, "body": "raw TAPD material"}}
        self.assertIsNone(native_preparation.github_effect_metadata(github_action, poisoned))
        self.assertIsNone(
            native_preparation.worker_tapd_body(
                {**tapd_action, "metadata_json": json.dumps({"target_id": "ticket", "body": "Changed README.md"})},
                result_json,
            )
        )
        path.write_text("Later unreviewed edit")
        self.git(tree, "add", "README.md")
        self.git(tree, "commit", "-m", tapd_policy.NATIVE_COMMIT_MESSAGE)
        self.assertFalse(native_preparation.recheck_github(audit["action_scope"]))

    def test_prelaunch_change_retires_exact_original_batch(self):
        self.config()
        first = self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.comments = [self.comment("1", suffix=" edited before launch"), self.comment("2")]
        replacement = self.tick("readmitted")
        self.assertNotEqual(replacement.round_id, first.round_id)
        self.assertEqual(
            self.read("SELECT count(*) FROM source_round_requests WHERE round_id=?", (replacement.round_id,)), [(1,)]
        )
        self.assertEqual(self.read("SELECT lifecycle FROM tasks"), [("canceled",)])
        self.tick("prepared")
        self.tick("dispatched")

    def test_worker_cancellation_retains_unknown_then_drains_and_readmits_remaining_batch(self):
        self.config()
        self.comments.append(self.comment("2"))
        first = self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.comments = [self.comment("1", marked=False), self.comments[1], self.comment("3")]
        held = self.tick("attention", terminate_worker=lambda _: {"status": "attention"})
        self.assertEqual(held.reason, "worker_exit_unconfirmed")
        self.assertEqual(self.read("SELECT status FROM source_rounds"), [("active",)])
        self.assertEqual(self.read("SELECT status FROM attempts"), [("running",)])
        self.tick("draining", terminate_worker=lambda _: {"status": "terminated"})
        self.tick("canceled")
        following = self.tick("admitted")
        self.assertNotEqual(first.round_id, following.round_id)
        self.assertEqual(
            self.read("SELECT count(*) FROM source_round_requests WHERE round_id=?", (following.round_id,)), [(2,)]
        )
        self.assertEqual(
            self.read("SELECT state FROM request_events ORDER BY state"), [("pending",), ("pending",), ("revoked",)]
        )

    def test_unknown_model_cancellation_releases_only_proven_group(self):
        self.config(two=True)
        self.stop_model = True
        self.tick("admitted", ticket=self.ticket)
        self.tick("attention")
        [[run_id, reserved]] = self.read("SELECT run_id,slot_reserved FROM source_model_runs")
        self.assertEqual(reserved, 1)
        self.comments = [self.comment("1", marked=False)]
        held = self.tick("attention", terminate_model=lambda _: {"status": "attention"})
        self.assertEqual(held.reason, "source_model_exit_unconfirmed")
        self.assertEqual(self.read("SELECT run_id,slot_reserved FROM source_model_runs"), [(run_id, 1)])
        self.tick("draining", terminate_model=lambda _: {"status": "terminated"})
        self.tick("canceled")
        self.assertEqual(
            self.read("SELECT run_id,status,slot_reserved FROM source_model_runs"), [(run_id, "interrupted", 0)]
        )
        self.assertEqual(len(self.model_calls), 1)

    def test_accepted_root_cancellation_drains_worker_preserving_required_action(self):
        from robert_agent.persistence.stores import tapd_intake as intake_store

        self.config(development=True)
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.tick("dispatched")
        self.submit("classification_result", recommended_route="comment-analysis")
        self.tick("waiting")
        self.tick("audited")
        self.tick("prepared")
        self.tick("dispatched")
        self.submit(
            "comment_analysis",
            planned_actions=[{"provider": "tapd", "type": "comment", "target_id": "ticket", "body": "Findings"}],
        )
        self.tick("waiting")
        self.tick("audited")
        root = self.tick("publication_pending")
        attempt_id = self.started[-1]["attempt_id"]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='running',metadata_json=json_remove(metadata_json,'$.process_stopped_at')"
                " WHERE attempt_id=?",
                (attempt_id,),
            )
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertTrue(
                intake_store.request_cancellation(
                    conn,
                    source_id=self.ticket.source_id,
                    round_id=root.round_id,
                    reason="fixture_pause",
                    now=values.utc_now(),
                )
            )
        held = self.tick("attention", paced=True, terminate_worker=lambda _: {"status": "attention"})
        self.assertEqual(held.reason, "worker_exit_unconfirmed")
        self.tick("draining", terminate_worker=lambda _: {"status": "terminated"})
        final = self.tick("held")
        self.assertEqual(final.reason, "source_publication_settlement_required")
        self.assertEqual(self.read("SELECT publish_status FROM github_actions"), [("not_published",)])
        self.assertEqual(self.read("SELECT state,source_result_id FROM request_events"), [("consumed", root.identity)])

    def test_waiting_attention_is_paced_before_polling_and_model_not_restarted(self):
        self.config(two=True)
        self.tick("admitted", ticket=self.ticket)
        self.model_unavailable = True
        outcome = self.tick("attention")
        self.assertEqual(outcome.reason, "unsupported_model_profile")
        self.assertEqual(
            self.read("SELECT status,reason FROM tapd_coordination"), [("attention", "unsupported_model_profile")]
        )
        reads = self.factory_calls
        self.tick("idle", paced=True)
        self.assertEqual(self.factory_calls, reads)
        self.tick("attention")
        self.assertEqual(self.factory_calls, reads)
        self.assertFalse(self.model_calls)

    def test_model_gate_observes_durable_revocation_of_another_frozen_member(self):
        from robert_agent.persistence.stores import tapd_intake as intake_store

        self.config(two=True)
        self.comments.append(self.comment("2"))
        round_ = self.tick("admitted", ticket=self.ticket)

        def revoke(_profile, _prompt, stop, started):
            identity = {
                "pid": 1234567,
                "process_group_id": 1234567,
                "identity_source": "proc",
                "process_start_token": "model-start",
                "command_fingerprint": "a" * 64,
            }
            self.assertTrue(started(identity))
            with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
                transactions.begin_immediate(conn)
                self.assertTrue(
                    intake_store.request_cancellation(
                        conn,
                        source_id=self.ticket.source_id,
                        round_id=round_.round_id,
                        reason="secondary_trigger_revoked",
                        now=values.utc_now(),
                    )
                )
            self.assertTrue(stop.is_set())
            return SourceModelOutcome("stopped", exit_kind="group_exited", identity=identity)

        with patch.object(source_models, "RECHECK_SECONDS", 0), patch.object(self, "model", new=revoke):
            self.tick("draining")
        self.assertEqual(
            self.read("SELECT status,slot_reserved,output_json FROM source_model_runs"), [("interrupted", 0, None)]
        )
        self.tick("canceled", paced=True)

    def test_disabled_configuration_drains_without_provider_access(self):
        self.config()
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        self.configured["tapd"]["enabled"] = False
        reads = self.factory_calls
        self.tick("draining")
        self.tick("canceled")
        self.assertEqual(self.factory_calls, reads)
        self.assertEqual(self.read("SELECT status FROM attempts"), [("canceled",)])

    def test_paused_mode_cannot_create_cancellation_or_use_provider(self):
        from robert_agent.persistence.stores import tapd_intake as intake_store

        self.config()
        round_ = self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        reads = self.factory_calls
        self.tick("idle", local_only=True)
        self.assertFalse(has_due_source(self.db, self.configured, owner="test-coordinator", local_only=True))
        self.assertEqual(self.read("SELECT count(*) FROM source_round_cancellations"), [(0,)])
        with closing(database.connect(self.db, foreign_keys=True)) as conn, conn:
            transactions.begin_immediate(conn)
            self.assertTrue(
                intake_store.request_cancellation(
                    conn,
                    source_id=self.ticket.source_id,
                    round_id=round_.round_id,
                    reason="secondary_trigger_revoked",
                    now=values.utc_now(),
                )
            )
        self.assertTrue(has_due_source(self.db, self.configured, owner="test-coordinator", local_only=True))
        self.tick("draining", local_only=True)
        self.assertEqual(self.factory_calls, reads)
        self.assertEqual(self.read("SELECT status FROM attempts"), [("canceled",)])

    def test_generic_ready_queue_excludes_source_prepared_attempts_before_capacity_limit(self):
        from robert_agent.persistence.queries import attempts

        self.config()
        self.tick("admitted", ticket=self.ticket)
        self.tick("prepared")
        [[repo_id]] = self.read("SELECT w.repo_id FROM workstreams w JOIN tasks t USING(workstream_id)")
        with closing(database.connect(self.db)) as conn:
            self.assertFalse(attempts.has_local_work_filtered_2(conn, [repo_id], values.utc_now()))
        route = self.read("SELECT route_id FROM tasks")[0][0]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at)"
                " VALUES('plain-stream',?,'2099-01-01','2099-01-01')",
                (repo_id,),
            )
            conn.execute(
                "INSERT INTO tasks(task_id,workstream_id,route_id,lifecycle,created_at,updated_at)"
                " VALUES('plain-task','plain-stream',?,'queued','2099-01-01','2099-01-01')",
                (route,),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,started_at,metadata_json)"
                " VALUES('plain-attempt','plain-task',1,'prepared','2099-01-01','{}')"
            )
            conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)"
                " VALUES('plain-prompt','plain-task','plain-attempt','prompt','/prompt','2099-01-01')"
            )
        with closing(database.connect(self.db)) as conn:
            self.assertTrue(attempts.has_local_work_filtered_2(conn, [repo_id], values.utc_now()))
            rows = attempts.find_for_queue_prepared_attempts(conn, repo_id, 1)
        self.assertEqual([row[0] for row in rows], ["plain-attempt"])

    def test_real_scan_control_child_revokes_separately_running_model(self):
        import os
        import sys
        import time

        from robert_agent.execution import runtime_state
        from robert_agent.runtime import control, native_sources

        self.config(two=True)
        self.tick("admitted", ticket=self.ticket)
        scan_config = self.root / "config.yml"
        raw = yaml.safe_load(scan_config.read_text())
        raw["data_dir"] = str(self.root)
        scan_config.write_text(yaml.safe_dump(raw))
        config_json = self.root / "normalized.json"
        config_json.write_text(json.dumps(self.configured))
        with closing(runtime_state.connect(self.db)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(
                conn,
                resource_key=f"database:{self.db}",
                owner_id="test-coordinator",
                ttl_seconds=60,
            )
        self.assertTrue(lease["ok"])

        model = control.ControlLane(self.db, native_sources.MODEL, "test-coordinator")
        scan = control.ControlLane(self.db, native_sources.SCAN, "test-coordinator")
        self.addCleanup(scan.stop)
        self.addCleanup(model.stop)
        model.start(
            [
                sys.executable,
                "-B",
                "-m",
                "tests.support.source_runtime_child",
                os.fspath(self.root),
                self.comments[0].version.created,
            ],
            timeout_seconds=25,
        )
        deadline = time.monotonic() + 10
        while not self.read("SELECT 1 FROM source_model_runs WHERE status='running'") and time.monotonic() < deadline:
            self.assertIsNone(model.poll(), "model wrapper exited before launch")
            time.sleep(0.025)
        self.assertEqual(self.read("SELECT status,slot_reserved FROM source_model_runs"), [("running", 1)])
        scan.start(
            native_sources.command(native_sources.SCAN, scan_config, self.db, "test-coordinator"), timeout_seconds=10
        )
        results = {}
        deadline = time.monotonic() + 10
        while len(results) < 2 and time.monotonic() < deadline:
            for name, lane in (("scan", scan), ("model", model)):
                if name not in results and (result := lane.poll()) is not None:
                    results[name] = result
            time.sleep(0.025)
        self.assertTrue(results["scan"]["ok"], results)
        self.assertTrue(results["model"]["ok"], results)
        self.assertEqual(self.read("SELECT reason FROM source_round_cancellations"), [("source_disabled",)])
        self.assertEqual(self.read("SELECT status,slot_reserved FROM source_model_runs"), [("interrupted", 0)])
        self.assertFalse(self.read("SELECT task_id FROM tasks"))

    def test_restart_keeps_orphan_model_reserved_until_exact_group_exit(self):
        import os
        import signal
        import sys
        import time

        from robert_agent.adapters.processes import identity as process_identity
        from robert_agent.adapters.processes.source_model import group_exited
        from robert_agent.execution import runtime_state
        from robert_agent.runtime import control, native_sources

        self.config(two=True)
        admitted = self.tick("admitted", ticket=self.ticket)
        (self.root / "normalized.json").write_text(json.dumps(self.configured))
        scope = f"database:{self.db}"
        with closing(runtime_state.connect(self.db)) as conn, conn:
            lease = runtime_state.acquire_daemon_lease(
                conn, resource_key=scope, owner_id="test-coordinator", ttl_seconds=60
            )
            runtime_state.start_daemon_run(conn, config_path="controlled", owner_id="test-coordinator")
        self.assertTrue(lease["ok"])
        old = control.ControlLane(self.db, native_sources.MODEL, "test-coordinator")
        self.addCleanup(old.stop)
        old.start(
            [
                sys.executable,
                "-B",
                "-m",
                "tests.support.source_runtime_child",
                str(self.root),
                self.comments[0].version.created,
            ],
            timeout_seconds=25,
        )
        deadline = time.monotonic() + 10
        while not self.read("SELECT 1 FROM source_model_runs WHERE status='running'") and time.monotonic() < deadline:
            self.assertIsNone(old.poll(), "source wrapper exited before recording its model")
            time.sleep(0.025)
        [[run_id, launch_id, dispatch_json, reserved]] = self.read(
            "SELECT run_id,launch_id,dispatch_json,slot_reserved FROM source_model_runs"
        )
        model_identity = json.loads(dispatch_json)
        self.addCleanup(process_identity.terminate_recorded_identity, model_identity)
        self.assertEqual(reserved, 1)
        self.assertEqual(process_identity.inspect_recorded_identity(model_identity), "alive_same")
        self.assertNotEqual(model_identity["process_group_id"], old.process.pid)

        # Crash the entire recorded control group. Its model owns a different
        # group and must remain independently accounted for after takeover.
        os.killpg(old.process.pid, signal.SIGKILL)
        old.process.wait(timeout=5)
        self.assertEqual(process_identity.inspect_recorded_identity(model_identity), "alive_same")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE lease_id=?", (lease["lease_id"],)
            )
            conn.execute("UPDATE source_sessions SET lease_until='2000-01-01T00:00:00+00:00'")
        with closing(runtime_state.connect(self.db)) as conn, conn:
            replacement = runtime_state.acquire_daemon_lease(
                conn, resource_key=scope, owner_id="restarted-controller", ttl_seconds=60
            )
            runtime_state.start_daemon_run(conn, config_path="controlled", owner_id="restarted-controller")
        self.assertTrue(replacement["ok"])
        self.assertTrue(control.recover(self.db, scope_key=scope, current_owner="restarted-controller"))

        with (
            patch.object(source_models, "select_worker", return_value=self.profile),
            patch.object(dispatch.dispatch, "dispatch_worker") as launch,
        ):

            def advance():
                return advance_source(
                    self.db,
                    self.root,
                    self.configured,
                    owner="restarted-controller",
                    ticket=self.ticket,
                    client_factory=self.factory,
                    observation_provider=self.provider,
                    model_runner=self.model,
                )

            blocked = advance()
            self.assertEqual((blocked.status, blocked.reason), ("attention", "source_model_exit_unconfirmed"))
            self.assertEqual(blocked.round_id, admitted.round_id)
            self.assertEqual(
                self.read("SELECT run_id,launch_id,slot_reserved,status FROM source_model_runs"),
                [(run_id, launch_id, 1, "running")],
            )
            self.assertFalse(self.read("SELECT task_id FROM tasks"))
            self.assertFalse(self.model_calls)
            self.assertEqual(self.read("SELECT owner,token FROM source_sessions"), [("restarted-controller", 2)])

            stopped = process_identity.terminate_recorded_identity(model_identity)
            self.assertIn(stopped["status"], {"terminated", "already_dead"})
            deadline = time.monotonic() + 5
            while not group_exited(model_identity) and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertTrue(group_exited(model_identity))
            recovered = advance()
            self.assertEqual(recovered.reason, "source_model_reconciled")
            self.assertEqual(
                self.read("SELECT run_id,launch_id,slot_reserved,status FROM source_model_runs"),
                [(run_id, launch_id, 0, "interrupted")],
            )
            self.assertFalse(self.model_calls)
            launch.assert_not_called()

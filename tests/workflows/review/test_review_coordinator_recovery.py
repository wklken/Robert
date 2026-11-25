from collections import Counter
from pathlib import Path
from unittest.mock import patch

from robert_agent.persistence.rounds import review as review_store
from robert_agent.runtime import loop as loop_engine
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.workflows.review import coordinator as review_coordinator
from tests.support import database as fixture_database
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ReviewCoordinatorRecoveryTests(ReviewCoordinatorFixture):
    def test_legacy_freeze_before_database_registration_resumes_reviewers(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        with patch.object(driver, "prepare_context"):
            driver.prepare()
        frozen = self.git_fixture.freeze_legacy(driver.manifest)
        driver.save_manifest()  # The database still has the unfrozen snapshot.
        before = Path(frozen["prompt_path"]).read_bytes()
        recovered = self.coordinator()
        recovered.prepare()
        self.assertEqual(self.calls, [])
        self.assertEqual(recovered.manifest, frozen)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(review_store.session(conn, task_id=driver.task_id)["manifest"], frozen)
        recovered.review()
        self.assertEqual(Counter(call[1] for call in self.calls), {"codex": 1, "claude": 1})
        self.assertTrue(all(call[2] == before for call in self.calls))

    def test_retrigger_superseded_review_creates_round_for_current_revision(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        old_manifest = dict(driver.manifest)
        self.git_fixture.commit("value = 3\n")
        with self.assertRaises(review_coordinator.StageSuperseded):
            driver.run()
        with fixture_database.connect(self.db) as conn:
            now = review_store.now()
            work_application_lifecycle.finalize_failed_task(
                conn, driver.task_id, driver.session["workstream_id"], now, {"status": "failed_worker_process_exited"}
            )
            conn.execute(
                "UPDATE attempts SET status='failed',finished_at=? WHERE attempt_id=?", (now, driver.attempt_id)
            )
            # The fixture launcher does not start a real coordinator process.
            conn.execute(
                "UPDATE attempts SET metadata_json=json_remove(metadata_json,'$.dispatch') WHERE attempt_id=?",
                (driver.attempt_id,),
            )
            repo_id = conn.execute(
                "SELECT repo_id FROM workstreams WHERE workstream_id=?", (driver.session["workstream_id"],)
            ).fetchone()[0]
        context = work_items.CommandContext("operator", "maintainer", frozenset({repo_id}), frozenset())
        work_items.retrigger_failed_github_task(
            self.db, driver.task_id, context=context, idempotency_key="retry-current-revision"
        )
        self.cycle([])
        retried = self.coordinator()
        self.assertNotEqual(retried.round_id, driver.round_id)
        self.assertTrue(retried.run().is_file())
        self.assertNotEqual(retried.manifest["head_sha"], old_manifest["head_sha"])
        self.assertEqual(retried.manifest["head_sha"], self.git_fixture.head)
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            original = review_store.session(conn, round_id=driver.round_id)
            self.assertEqual(original["status"], "superseded")
            self.assertEqual(original["task_id"], driver.task_id)
            self.assertEqual(original["manifest"], old_manifest)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_retrigger_failed_recovery_round_reuses_completed_review_stages(self):
        def fail_task(driver):
            with fixture_database.connect(self.db) as conn:
                now = review_store.now()
                work_application_lifecycle.finalize_failed_task(
                    conn,
                    driver.task_id,
                    driver.session["workstream_id"],
                    now,
                    {"status": "failed_worker_process_exited"},
                )
                conn.execute(
                    "UPDATE attempts SET status='failed',finished_at=?,metadata_json=json_remove(metadata_json,'$.dispatch') "
                    "WHERE attempt_id=?",
                    (now, driver.attempt_id),
                )
                repo_id = conn.execute(
                    "SELECT repo_id FROM workstreams WHERE workstream_id=?", (driver.session["workstream_id"],)
                ).fetchone()[0]
            return work_items.CommandContext("operator", "maintainer", frozenset({repo_id}), frozenset())

        self.cycle([self.event()])
        original = self.coordinator()
        original.prepare()
        original_manifest = dict(original.manifest)
        self.git_fixture.commit("value = 3\n")
        with self.assertRaises(review_coordinator.StageSuperseded):
            original.run()
        context = fail_task(original)
        work_items.retrigger_failed_github_task(
            self.db, original.task_id, context=context, idempotency_key="retry-superseded-revision"
        )
        self.cycle([])
        recovered = self.coordinator()
        with patch.object(recovered, "synthesize", side_effect=RuntimeError("test synthesis interrupted")):
            with self.assertRaisesRegex(RuntimeError, "test synthesis interrupted"):
                recovered.run()
        counts = Counter((call[0], call[1]) for call in self.calls)
        manifest = dict(recovered.manifest)
        context = fail_task(recovered)
        work_items.retrigger_failed_github_task(
            self.db, recovered.task_id, context=context, idempotency_key="retry-interrupted-recovery-round"
        )
        self.cycle([])
        retried = self.coordinator()
        self.assertNotEqual(retried.task_id, recovered.task_id)
        self.assertEqual(retried.round_id, recovered.round_id)
        self.assertTrue(retried.run().is_file())
        self.assertEqual(retried.manifest, manifest)
        counts[("synthesis", "codex")] += 1
        self.assertEqual(Counter((call[0], call[1]) for call in self.calls), counts)
        with fixture_database.connect(self.db) as conn:
            saved_original = review_store.session(conn, round_id=original.round_id)
            self.assertEqual(saved_original["status"], "superseded")
            self.assertEqual(saved_original["task_id"], original.task_id)
            self.assertEqual(saved_original["manifest"], original_manifest)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_file_landed_before_registration_is_adopted(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        original_register = driver.register

        def crash(run, artifact):
            if run["stage"] == "review":
                raise KeyboardInterrupt("simulated crash after file rename")
            return original_register(run, artifact)

        with patch.object(driver, "register", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            driver.review()
        self.coordinator().run()
        self.assertEqual(Counter((c[0], c[1]) for c in self.calls)[("review", "codex")], 1)

    def test_tampered_success_reruns_only_affected_model(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        Path(records[0]["result_path"]).write_text("{}")
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[("review", "codex")], 2)
        self.assertEqual(counts[("review", "claude")], 1)

    def test_synthesis_retry_does_not_rerun_reviewers(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def fail_synthesis(**kwargs):
            if Path(kwargs["prompt_path"]).name.startswith("synthesis-prompt-"):
                return {"ok": False, "status": "failed_dispatch", "safe_error": "synthesis unavailable"}
            return original(**kwargs)

        driver.launcher = fail_synthesis
        with self.assertRaisesRegex(RuntimeError, "synthesis unavailable"):
            driver.run()
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[("review", "codex")], 1)
        self.assertEqual(counts[("review", "claude")], 1)

    def test_unbound_queue_after_crash_wakes_local_drain_in_repo_scope(self):
        self.cycle([self.event()])
        task, _ = self.current()
        self.cycle([self.event("second")])
        self.finish_task(task)
        with fixture_database.connect(self.db) as conn:
            repo_id = conn.execute("SELECT repo_id FROM repos").fetchone()[0]
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_id=repo_id))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id="different-repo"))
            conn.execute("UPDATE review_sessions SET status='canceled' WHERE task_id IS NULL")
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_id=repo_id))

    def test_changed_result_after_failed_synthesis_gets_new_sealed_inputs(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        driver.launcher = lambda **kwargs: {
            "ok": False,
            "status": "failed_dispatch",
            "safe_error": "synthesis unavailable",
        }
        with self.assertRaisesRegex(RuntimeError, "synthesis unavailable"):
            driver.synthesize(records)
        original = Path(driver.manifest["artifact_root"], "synthesis", "inputs.json").read_bytes()
        Path(records[0]["result_path"]).write_text("{}")
        self.coordinator().run()
        self.assertEqual(Path(driver.manifest["artifact_root"], "synthesis", "inputs.json").read_bytes(), original)
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[("review", "codex")], 2)
        self.assertEqual(counts[("review", "claude")], 1)

    def test_owner_attention_blocks_next_round_even_if_session_status_is_old(self):
        self.cycle([self.event()])
        task, _ = self.current()
        self.cycle([self.event("second")])
        self.finish_task(task)
        with fixture_database.connect(self.db) as conn:
            conn.execute("UPDATE tasks SET lifecycle='failed' WHERE task_id=?", (task,))
            conn.execute("UPDATE execution_subjects SET binding_status='attention' WHERE subject_id=?", (task,))
            conn.execute("UPDATE review_sessions SET status='reviewing' WHERE task_id=?", (task,))
            self.assertFalse(loop_engine.has_runnable_local_work(conn))
        self.cycle([])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_unregistered_old_synthesis_is_not_adopted_after_input_repair(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        with patch.object(driver, "register", side_effect=KeyboardInterrupt("crash before synthesis registration")):
            with self.assertRaises(KeyboardInterrupt):
                driver.synthesize(records)
        Path(records[0]["result_path"]).write_text("{}")
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[("synthesis", "codex")], 2)
        with fixture_database.connect(self.db) as conn:
            syntheses = [r for r in review_store.runs(conn, driver.round_id) if r["stage"] == "synthesis"]
        self.assertEqual(len(syntheses), 2)
        self.assertNotEqual(syntheses[0]["prompt_sha256"], syntheses[1]["prompt_sha256"])

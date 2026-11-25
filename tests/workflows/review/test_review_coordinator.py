from tests.support import database as fixture_database
from robert_agent.adapters.processes import identity as worker_identity
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch


from robert_agent.delivery.application import publish
from robert_agent.workflows.review import coordinator as review_coordinator
from robert_agent.workflows.review import files as review_files
from robert_agent.persistence.rounds import review as review_store
from robert_agent.work.application import commands as work_items
from robert_agent.execution.application import evidence
from robert_agent.execution.application import monitor
from robert_agent.work.application import preparation
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ReviewCoordinatorTests(ReviewCoordinatorFixture):
    def test_tab_git_path_does_not_block_either_reviewer(self):
        (self.git_fixture.upstream / "tab\tfile.py").write_text("value = 1\n")
        self.git_fixture.git(self.git_fixture.upstream, "add", ".")
        self.git_fixture.commit("value = 3\n")
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        driver.review()
        self.assertEqual(Counter(call[1] for call in self.calls), {"codex": 1, "claude": 1})
        self.assertEqual(self.calls[0][2], self.calls[1][2])

    def test_fresh_preparation_freezes_context_without_models_or_slots(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        self.assertEqual(self.calls, [])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(review_store.runs(conn, driver.round_id), [])
            self.assertEqual(review_store.process_pool_status(conn)["in_use"], 0)
        self.assertTrue(driver.manifest["frozen"])
        context = Path(driver.manifest["context_dir"])
        summary = (context / "context-summary.md").read_text()
        self.assertIn(driver.manifest["head_sha"], summary)
        self.assertIn("review this PR", summary)
        index = json.loads((context / "source-index.json").read_text())
        self.assertIn("source.py", [entry["path"] for entry in index["files"]])
        self.assertEqual(index["preparation_mode"], "controller-inventory")
        self.assertIn("not read", index["reading_notes"])

    def test_file_handoff_same_prompt_and_two_independent_rounds(self):
        self.cycle([self.event()])
        first = self.coordinator()
        report1 = first.run()
        self.assertTrue(report1.is_file())
        self.assertEqual(first.manifest["github_account"], "robert-bot")
        self.assertEqual(first.manifest["decision_owner"]["login"], "maintainer")
        reviewer_calls = [call for call in self.calls if call[0] == "review"]
        self.assertEqual(reviewer_calls[0][2:4], reviewer_calls[1][2:4])
        self.assertIn(b'Configured Robert GitHub account: "robert-bot"', reviewer_calls[0][2])
        self.assertIn(b'"decision_mention": "@maintainer"', reviewer_calls[0][2])
        self.assertNotEqual(reviewer_calls[0][4], reviewer_calls[1][4])
        self.assertIn("Code Review 汇总报告", report1.read_text())
        phases = {"prepare": "review.prepare", "review": "review.evaluate", "synthesis": "review.synthesize"}
        for phase, _, prompt, *_ in self.calls:
            self.assertIn(f"Stage: {phases[phase]}".encode(), prompt)
            self.assertIn(b"## Context", prompt)
            self.assertIn(b"## Delivery", prompt)
        self.cycle([self.event("second"), self.event("second")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM task_events WHERE relationship='pending'").fetchone()[0], 0
            )
        self.finish_task(first.task_id)
        self.git_fixture.commit("value = 3\n")
        self.cycle([])  # local drain starts the already queued request, no new GitHub event
        second = self.coordinator()
        report2 = second.run()
        self.assertNotEqual(first.round_id, second.round_id)
        self.assertNotEqual(first.manifest["worktree_path"], second.manifest["worktree_path"])
        self.assertNotEqual(first.manifest["head_sha"], second.manifest["head_sha"])
        self.assertNotEqual(report1, report2)
        self.assertIn("value = 2", Path(first.manifest["worktree_path"], "source.py").read_text())

    def test_report_is_normalized_before_hash_registration_and_submission(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = review_files.render_merged

        def report(*args):
            return original(*args) + "\nExample: password=secret; path /root/example/report; host 10.0.0.12\n"

        with patch.object(review_files, "render_merged", side_effect=report):
            path = driver.run()
        body = path.read_text()
        self.assertIn("password=<redacted>", body)
        self.assertIn("<local-path>", body)
        self.assertIn("<internal-ip>", body)
        with fixture_database.connect(self.db) as conn:
            session = review_store.session(conn, task_id=driver.task_id)
            self.assertEqual(session["report_sha256"], review_store.digest(path))
            action = json.loads(
                conn.execute("SELECT metadata_json FROM github_actions WHERE task_id=?", (driver.task_id,)).fetchone()[
                    0
                ]
            )
            self.assertEqual(action["body"], body)
            self.assertEqual(action["report_sha256"], session["report_sha256"])

    def test_blocked_report_retains_model_artifacts_and_stops_automatic_recovery(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = review_files.render_merged

        def report(*args):
            return original(*args) + "\npassword=private-example-value\n"

        with patch.object(review_files, "render_merged", side_effect=report):
            with self.assertRaisesRegex(RuntimeError, "redaction_blocked.*credential_assignment.*line"):
                driver.run()
        with fixture_database.connect(self.db) as conn:
            session = review_store.session(conn, task_id=driver.task_id)
            self.assertEqual(session["status"], "attention")
            self.assertNotIn("private-example-value", session["error"])
            self.assertTrue(all(run["status"] == "succeeded" for run in review_store.runs(conn, driver.round_id)))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            recovery = evidence.inspect_attempt_recovery(
                conn, driver.attempt_id, driver.task_id, driver.manifest["worktree_path"]
            )
            self.assertEqual(recovery["status"], "unrecoverable")
            self.assertEqual(recovery["reason_code"], "redaction_blocked")
        # Explicit retry after repairing the cause reuses every sealed model stage.
        counts = Counter((call[0], call[1]) for call in self.calls)
        # A repaired renderer is a different code version. Patching a function
        # alone does not change the real module bytes used as the version pin.
        with patch.object(review_coordinator.report_checkpoint, "code_version", return_value="repaired-renderer-v2"):
            self.coordinator().run()
        self.assertEqual(Counter((call[0], call[1]) for call in self.calls), counts)

    def test_daemon_preserves_submission_rejection_in_task_failure_and_notification(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        diagnostic = {
            "ok": False,
            "status": "failed_validation",
            "reason_code": "redaction_blocked",
            "field_path": "planned_github_actions[0].body",
            "safe_error": "credential_assignment at line 2, column 1",
            "findings": [{"rule": "credential_assignment", "line": 2, "column": 1}],
            "required_action": "Redact the report.",
        }
        with patch.object(review_coordinator.worker_result, "record_result", return_value=diagnostic):
            with self.assertRaisesRegex(RuntimeError, "redaction_blocked"):
                driver.run()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(process.kill)
        self.addCleanup(process.wait, 5)
        identity = worker_identity.capture_process_identity(process.pid)
        process.kill()
        process.wait(timeout=5)
        with fixture_database.connect(self.db) as conn:
            now = review_store.now()
            conn.execute(
                """UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,
                metadata_json=json_set(metadata_json,'$.dispatch',json(?)) WHERE attempt_id=?""",
                (now, now, json.dumps(identity), driver.attempt_id),
            )
            conn.execute("UPDATE tasks SET lifecycle='running' WHERE task_id=?", (driver.task_id,))
            repo_id = conn.execute(
                "SELECT repo_id FROM workstreams WHERE workstream_id=?", (driver.session["workstream_id"],)
            ).fetchone()[0]
        with fixture_database.connect(self.db) as conn:
            monitor.supervise_running_attempts(
                conn,
                repo_id,
                {},
                now,
                data_dir=self.db.parent,
                db_path=self.db,
                repo={},
                resume_attempt=preparation.create_resume_attempt_and_prompt,
            )
            failure = json.loads(
                conn.execute("SELECT failure_json FROM attempts WHERE attempt_id=?", (driver.attempt_id,)).fetchone()[0]
            )
            self.assertEqual(failure["status"], "failed_validation")
            self.assertEqual(failure["reason_code"], "redaction_blocked")
            self.assertEqual(failure["findings"], diagnostic["findings"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
            notification = json.loads(
                conn.execute("""SELECT metadata_json FROM notifications
                WHERE notification_type='worker_result_validation_failed'""").fetchone()[0]
            )
            self.assertEqual(notification["reason_code"], "redaction_blocked")
        counts = Counter((call[0], call[1]) for call in self.calls)
        context = work_items.CommandContext("operator", "maintainer", frozenset({repo_id}), frozenset())
        work_items.retrigger_failed_github_task(
            self.db, driver.task_id, context=context, idempotency_key="retry-redacted-report"
        )
        self.cycle([])
        retried = self.coordinator()
        self.assertNotEqual(retried.task_id, driver.task_id)
        self.assertEqual(retried.round_id, driver.round_id)
        self.assertTrue(retried.run().is_file())
        self.assertEqual(Counter((call[0], call[1]) for call in self.calls), counts)

    def test_advanced_target_branch_completes_review_at_captured_pr_base(self):
        self.cycle([self.event()])
        self.git_fixture.git(self.git_fixture.upstream, "checkout", "main")
        (self.git_fixture.upstream / "other.py").write_text("unrelated = True\n")
        self.git_fixture.git(self.git_fixture.upstream, "add", ".")
        self.git_fixture.git(self.git_fixture.upstream, "commit", "-m", "advance target branch")

        driver = self.coordinator()
        report = driver.run()

        self.assertTrue(report.is_file())
        self.assertEqual(driver.manifest["base_sha"], self.git_fixture.base)
        self.assertTrue(review_coordinator.same_revision(driver.manifest, self.pr("", 42)))
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(review_store.session(conn, round_id=driver.round_id)["status"], "ready")

    def test_sealing_rejects_fifo_without_waiting_for_a_writer(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        with fixture_database.connect(self.db) as conn:
            run = review_store.new_run(
                conn,
                driver.round_id,
                "review",
                "codex",
                driver.attempt_id,
                driver.manifest["artifact_root"],
                driver.manifest["prompt_sha256"],
            )
        os.mkfifo(Path(run["output_dir"], "result.json"))
        script = """import json,sys
from robert_agent.workflows.review.coordinator import Coordinator
driver=object.__new__(Coordinator)
driver.manifest=json.loads(sys.argv[1])
try:
    driver.sealed_output(json.loads(sys.argv[2]))
except ValueError as error:
    assert 'regular file' in str(error), error
else:
    raise AssertionError('FIFO accepted')
"""
        result = subprocess.run(
            [sys.executable, "-c", script, json.dumps(driver.manifest), json.dumps(run)],
            timeout=2,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stdout_only_and_wrong_round_never_publish(self):
        self.cycle([self.event()])
        self.stdout_only = True
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            self.coordinator().run()
        self.stdout_only, self.bad_round = False, True
        with self.assertRaises((ValueError, RuntimeError, OSError)):
            self.coordinator().run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)

    def test_late_producer_write_cannot_change_registered_artifacts(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        before = [Path(record["result_path"]).read_bytes() for record in records]
        for call in self.calls:
            if call[0] == "review":
                Path(call[4], "result.json").write_text("late producer write")
        driver.synthesize(records)
        self.assertEqual([Path(record["result_path"]).read_bytes() for record in records], before)

    def test_record_audit_publish_finalize_and_drain_next_round(self):
        self.cycle([self.event()])
        first = self.coordinator()
        first.run()
        self.cycle([self.event("second")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT audit_status FROM github_actions").fetchone()[0], "accepted")
        calls = []

        def api(command, **kwargs):
            calls.append(command)
            if "--paginate" in command:
                value = [[]]
            elif "--method" not in command:
                value = self.pr("", 42)
            else:
                value = {
                    "id": 100,
                    "commit_id": first.manifest["head_sha"],
                    "state": "COMMENTED",
                    "body": next(arg[5:] for arg in command if arg.startswith("body=")),
                    "html_url": "https://github.com/example/backend/pull/42#pullrequestreview-100",
                }
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        result = publish.publish_ready_actions(self.db, run_command=api)
        self.assertEqual(result["published_count"], 1, result)
        self.assertEqual(sum("--method" in call for call in calls), 1)
        self.cycle([], skip_publish=False)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks WHERE task_id=?", (first.task_id,)).fetchone()[0], "completed"
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            self.assertEqual(review_store.session(conn, task_id=first.task_id)["status"], "published")

    def test_superseded_report_releases_queue_without_publication(self):
        self.cycle([self.event()])
        first = self.coordinator()
        first.run()
        self.cycle([self.event("second")])
        self.git_fixture.commit("value = 4\n")

        def api(command, **kwargs):
            self.assertNotIn("--method", command)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps([[]] if "--paginate" in command else self.pr("", 42)), stderr=""
            )

        outcome = publish.publish_ready_actions(self.db, run_command=api)
        self.assertEqual(outcome["skipped_count"], 1, outcome)
        self.cycle([], skip_publish=False)
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(review_store.session(conn, task_id=first.task_id)["status"], "superseded")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

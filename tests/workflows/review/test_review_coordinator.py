from tests.support import database as fixture_database
from robert_agent.workflows import routing as workflow_routing
from robert_agent.adapters.processes import identity as worker_identity
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from robert_agent.runtime import loop as loop_engine
from robert_agent.delivery.application import publish
from robert_agent.workflows.review import coordinator as review_coordinator
from robert_agent.workflows.review import files as review_files
from robert_agent.persistence.rounds import review as review_store
from robert_agent.runtime import cycle as run_once
from robert_agent.configuration import command as validate_config
from robert_agent.work.application import commands as work_items
from robert_agent.work.application import lifecycle as work_application_lifecycle
from robert_agent.resource_files import resource
from robert_agent.execution.application import evidence
from robert_agent.execution.application import monitor
from robert_agent.work.application import remediation_requests
from robert_agent.work.application import source_lifecycle
from robert_agent.work.application import preparation
from tests.support.review_coordinator import ReviewCoordinatorFixture


class ReviewCoordinatorTests(ReviewCoordinatorFixture):
    def test_file_handoff_same_prompt_and_two_independent_rounds(self):
        self.cycle([self.event()])
        first = self.coordinator()
        report1 = first.run()
        self.assertTrue(report1.is_file())
        self.assertEqual(first.manifest["github_account"], "robert-bot")
        reviewer_calls = [call for call in self.calls if call[0] == "review"]
        self.assertEqual(reviewer_calls[0][2:4], reviewer_calls[1][2:4])
        self.assertIn(b'Configured Robert GitHub account: "robert-bot"', reviewer_calls[0][2])
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

    def test_failed_model_retry_reuses_other_result_and_exact_prompt(self):
        self.cycle([self.event()])
        self.fail_worker = "claude"
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.coordinator().run()
        self.fail_worker = None
        self.coordinator().run()
        counts = Counter((call[0], call[1]) for call in self.calls)
        self.assertEqual(counts[("prepare", "codex")], 1)
        self.assertEqual(counts[("review", "codex")], 1)
        self.assertEqual(counts[("review", "claude")], 2)
        self.assertEqual(counts[("synthesis", "codex")], 1)
        prompts = [call[2] for call in self.calls if call[0] == "review"]
        self.assertEqual(len(set(prompts)), 1)

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

    def test_timeout_fallback_is_per_reviewer_and_survives_file_recovery(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"]["model"] = "primary-model"
        config["workers"]["claude"]["fallback_model"] = "fallback-model"
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        original_wait = review_coordinator.Coordinator._wait

        def timeout(driver, run, launched, seconds):
            if run["stage"] == "review" and run["worker_id"] == "claude":
                raise RuntimeError("review worker timed out")
            return original_wait(driver, run, launched, seconds)

        with patch.object(review_coordinator.Coordinator, "_wait", timeout):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    self.coordinator().run()
        driver = self.coordinator()
        original_register = driver.register

        def crash(run, artifact):
            if run["stage"] == "review" and run["worker_id"] == "claude":
                raise KeyboardInterrupt("fallback file before registration")
            return original_register(run, artifact)

        with patch.object(driver, "register", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            driver.run()
        report = self.coordinator().run()
        reviews = [call for call in self.calls if call[0] == "review"]
        self.assertEqual(
            [call[5] for call in reviews if call[1] == "claude"], ["primary-model", "primary-model", "fallback-model"]
        )
        self.assertEqual(sum(call[1] == "codex" for call in reviews), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertIn("fallback-model", report.read_text())
        with fixture_database.connect(self.db) as conn:
            selected = [
                run
                for run in review_store.runs(conn, driver.round_id)
                if run["stage"] == "review" and run["status"] == "succeeded"
            ]
            fallback = next(run for run in selected if run["worker_id"] == "claude")
            self.assertEqual(fallback["model"], "fallback-model")
            self.assertEqual(fallback["artifact"]["model"], "fallback-model")
            self.assertIn("fallback-model", json.dumps(review_store.board_status(conn, driver.task_id)))

    def test_rate_limit_automatically_switches_only_affected_reviewer(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude" and kwargs["model"] == "primary-model":
                output = Path(kwargs["log_dir"])
                log = output / "stdout.log"
                log.write_text(
                    json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}) + "\n"
                )
                self.calls.append(
                    (
                        "review",
                        "claude",
                        Path(kwargs["prompt_path"]).read_bytes(),
                        kwargs["worktree_path"],
                        str(output),
                        kwargs["model"],
                    )
                )
                return {
                    "ok": False,
                    "pid": 1073741824,
                    "process_exited": True,
                    "returncode": 1,
                    "stdout_path": str(log),
                    "safe_error": "worker exited with code 1",
                }
            return original(**kwargs)

        driver.launcher = limited
        report = driver.run()
        reviews = [call for call in self.calls if call[0] == "review"]
        self.assertEqual([call[5] for call in reviews if call[1] == "claude"], ["primary-model", "fallback-model"])
        self.assertEqual(sum(call[1] == "codex" for call in reviews), 1)
        self.assertEqual(len({call[2] for call in reviews}), 1)
        self.assertIn("fallback-model", report.read_text())

    def test_rate_limited_fallback_stops_and_survives_coordinator_restart(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        limited_models = []

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude":
                limited_models.append(kwargs["model"])
                log = Path(kwargs["log_dir"]) / "stderr.log"
                log.write_text("HTTP 429 Too Many Requests")
                return {
                    "ok": False,
                    "pid": 1073741824,
                    "process_exited": True,
                    "returncode": 1,
                    "stderr_path": str(log),
                    "safe_error": "worker exited with code 1",
                }
            return original(**kwargs)

        driver.launcher = limited
        with self.assertRaisesRegex(RuntimeError, "rate_limited"):
            driver.run()
        restarted = self.coordinator()
        restarted.launcher = limited
        with self.assertRaisesRegex(RuntimeError, "rate_limited"):
            restarted.run()
        self.assertEqual(limited_models, ["primary-model", "fallback-model"])

    def test_rate_limit_switch_after_running_worker_exits(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["workers"]["claude"].update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher
        models = []

        def limited(**kwargs):
            if kwargs["worker_agent_config"]["name"] == "claude":
                models.append(kwargs["model"])
                if kwargs["model"] == "primary-model":
                    log = Path(kwargs["log_dir"]) / "stdout.log"
                    log.write_text(json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}}))
                    return {
                        "ok": True,
                        "pid": 1073741824,
                        "process_exited": True,
                        "returncode": 1,
                        "stdout_path": str(log),
                    }
            return original(**kwargs)

        driver.launcher = limited
        self.assertTrue(driver.run().is_file())
        self.assertEqual(models, ["primary-model", "fallback-model"])

    def test_rate_limit_without_distinct_fallback_does_not_relaunch_stage(self):
        for fallback in (None, "primary-model"):
            with self.subTest(fallback=fallback):
                fixture = ReviewCoordinatorTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = yaml.safe_load(fixture.config_path.read_text())
                config["workers"]["claude"].update(model="primary-model", fallback_model=fallback)
                if fallback is None:
                    config["workers"]["claude"].pop("fallback_model")
                fixture.config_path.write_text(yaml.safe_dump(config))
                fixture.cycle([fixture.event()])
                driver = fixture.coordinator()
                original = driver.launcher
                calls = []

                def limited(**kwargs):
                    if kwargs["worker_agent_config"]["name"] == "claude":
                        calls.append(kwargs["model"])
                        log = Path(kwargs["log_dir"]) / "stdout.log"
                        log.write_text(
                            json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}})
                        )
                        return {
                            "ok": False,
                            "pid": 1073741824,
                            "process_exited": True,
                            "returncode": 1,
                            "stdout_path": str(log),
                            "safe_error": "worker exited with code 1",
                        }
                    return original(**kwargs)

                driver.launcher = limited
                with self.assertRaisesRegex(RuntimeError, "rate_limited"):
                    driver.run()
                self.assertEqual(calls, ["primary-model"])

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

    def test_other_reviewers_timeouts_do_not_advance_fallback(self):
        config = yaml.safe_load(self.config_path.read_text())
        for worker in config["workers"].values():
            worker.update(model="primary-model", fallback_model="fallback-model")
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        original_wait = review_coordinator.Coordinator._wait

        def timeout(driver, run, launched, seconds):
            if run["stage"] == "review":
                raise RuntimeError("review worker timed out")
            return original_wait(driver, run, launched, seconds)

        with patch.object(review_coordinator.Coordinator, "_wait", timeout):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.coordinator().run()
        self.coordinator().run()
        self.assertEqual({call[5] for call in self.calls if call[0] == "review"}, {"primary-model"})

    def test_assignment_then_comment_runs_two_binaries_and_publishes_each_revision_once(self):
        binary = self.root / "file_reviewer.py"
        binary.write_text("""import hashlib,json,os,sys
from pathlib import Path
prompt=sys.stdin.buffer.read()
text=prompt.decode()
stage_id=next(line.removeprefix('Stage: ') for line in text.splitlines() if line.startswith('Stage: '))
def required_path(item):
    section=text.split('\\n- '+item+' (required, file, complete):',1)[1].split('\\n- ',1)[0]
    line=next(line for line in section.splitlines() if line.startswith('  Read '))
    return Path(line.removeprefix('  Read ').removesuffix(' before acting.'))
manifest_path=required_path('manifest.json')
manifest=json.loads(manifest_path.read_text())
context=manifest_path.parent
source=Path(manifest['worktree_path'],'source.py').read_text()
assert Path.cwd()==Path(manifest['worktree_path'])
assert source.strip() in (context/'diff.patch').read_text()
output=Path(os.environ['ROBERT_REVIEW_OUTPUT_DIR'])
identity={key:manifest[key] for key in ('round_id','snapshot_id','head_sha')}
def save(name,value):
    path=output/(name+'.tmp')
    path.write_text(json.dumps(value))
    path.replace(output/name)
selected=[]
if stage_id=='review.prepare':
    stage='prepare'
    (output/'context-summary.md').write_text(source)
    save('source-index.json',{'files':[{'path':'source.py','reason':'changed source'}],'reading_notes':source})
elif stage_id=='review.evaluate':
    stage='review'
    assert (context/'context-summary.md').read_text()==source
    assert json.loads((context/'source-index.json').read_text())['reading_notes']==source
    save('result.json',dict(identity,schema_version=1,findings=[],dismissed_findings=[],
        coverage={'reviewed_paths':['source.py'],'unreviewed_paths':[],'notes':source},merge_recommendation='ready to merge'))
else:
    assert stage_id=='review.synthesize'
    stage='synthesis'
    inputs=required_path('selected-reviewer-results')
    records=json.loads(inputs.read_text())['records']
    assert len(records)==2
    for record in records:
        result=json.loads(Path(record['result_path']).read_text())
        assert result['coverage']['notes']==source and result['head_sha']==identity['head_sha']
        assert Path(record['report_path']).read_text()
        selected.append(record['result_path'])
    save('evaluation.json',dict(identity,schema_version=1,items=[],merge_recommendation='ready to merge',merge_reason='无问题'))
save('observed.json',dict(identity,stage=stage,source=source,prompt_sha256=hashlib.sha256(prompt).hexdigest(),selected=selected))
""")
        config = yaml.safe_load(self.config_path.read_text())
        for worker in config["workers"].values():
            worker["command"] = shlex.join([sys.executable, str(binary)])
        self.config_path.write_text(yaml.safe_dump(config))
        first_event = self.event() | {
            "event_type": "assigned",
            "actor_login": "robert-bot",
            "assignment_actor_login": "maintainer",
            "assigned_to": "robert-bot",
            "body": "",
            "intent": "unclear",
        }
        remote_reviews, rounds = [], []

        def api(command, **kwargs):
            if "--paginate" in command:
                value = [remote_reviews]
            elif "--method" not in command:
                value = self.pr("", 42)
            else:
                value = {
                    "id": 100 + len(remote_reviews),
                    "state": "COMMENTED",
                    "commit_id": next(arg[10:] for arg in command if arg.startswith("commit_id=")),
                    "body": next(arg[5:] for arg in command if arg.startswith("body=")),
                }
                value["html_url"] = f"https://github.com/example/backend/pull/42#pullrequestreview-{value['id']}"
                remote_reviews.append(value)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        for event, value in ((first_event, 2), (self.event("second"), 3)):
            if value == 3:
                self.git_fixture.commit("value = 3\n")
            self.cycle([event, event])
            task, attempt = self.current()
            driver = review_coordinator.Coordinator(self.db, task, attempt, fetch_pr=self.pr)
            report = driver.run()
            rounds.append((driver, report.read_bytes()))
            self.cycle([])
            result = publish.publish_ready_actions(self.db, run_command=api)
            self.assertEqual(result["published_count"], 1, result)
            self.assertEqual(publish.publish_ready_actions(self.db, run_command=api)["published_count"], 0)
            self.cycle([], skip_publish=False)
            with fixture_database.connect(self.db) as conn:
                session = review_store.session(conn, task_id=task)
                self.assertEqual(session["status"], "published")
                runs = review_store.runs(conn, driver.round_id)
            observed = [json.loads(Path(run["output_dir"], "observed.json").read_text()) for run in runs]
            reviews = [item for item in observed if item["stage"] == "review"]
            self.assertEqual(len(reviews), 2)
            self.assertEqual(len({item["prompt_sha256"] for item in reviews}), 1)
            self.assertEqual({item["source"] for item in observed}, {f"value = {value}\n"})
            self.assertTrue(all(driver.round_id in path for item in observed for path in item["selected"]))
        self.cycle([first_event, self.event("second")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        self.assertEqual(len(remote_reviews), 2)
        self.assertEqual(
            [item["commit_id"] for item in remote_reviews], [driver.manifest["head_sha"] for driver, _ in rounds]
        )
        self.assertNotEqual(rounds[0][0].manifest["head_sha"], rounds[1][0].manifest["head_sha"])
        self.assertNotEqual(rounds[0][0].manifest["worktree_path"], rounds[1][0].manifest["worktree_path"])
        self.assertEqual(
            Path(rounds[0][0].manifest["artifact_root"], "synthesis", "merged-review.md").read_bytes(), rounds[0][1]
        )

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

    def test_non_allowlisted_pr_author_keeps_legacy_worker(self):
        result = self.cycle([self.event(author="other")])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT expected_output FROM tasks").fetchone()[0], "pr_review_comment")
        self.assertEqual(result["dispatch_count"], 1)

    def test_new_same_sha_trigger_is_new_round_and_untrusted_trigger_is_not(self):
        self.cycle([self.event(), self.event("untrusted", actor="stranger")])
        self.cycle([self.event(), self.event("second")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_enabling_native_review_does_not_replay_consumed_legacy_triggers(self):
        for event_type in ("comment", "review_request"):
            with self.subTest(event_type=event_type):
                fixture = ReviewCoordinatorTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                config = yaml.safe_load(fixture.config_path.read_text())
                config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
                fixture.config_path.write_text(yaml.safe_dump(config))
                event = fixture.event("legacy-" + event_type)
                if event_type == "review_request":
                    event.update(
                        event_type="review_request",
                        body="",
                        requested_reviewer="robert-bot",
                        requester_login="maintainer",
                    )
                fixture.cycle([event])
                task, _ = fixture.current()
                fixture.finish_task(task)
                with fixture_database.connect(fixture.db) as conn:
                    conn.execute("UPDATE task_events SET relationship='consumed' WHERE task_id=?", (task,))
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task,)).fetchone()[0], 1
                    )
                    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
                fixture.config_path.write_text(yaml.safe_dump(config))

                fixture.cycle([event, event])

                with fixture_database.connect(fixture.db) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)

    def test_queued_legacy_replay_is_canceled_without_creating_a_task(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([self.event()])
        task, _ = self.current()
        self.finish_task(task)
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(config))
        runtime = validate_config.validate_config(self.config_path, skip_external=True)
        repo = runtime["repos"][0]
        with fixture_database.connect(self.db) as conn:
            event = json.loads(conn.execute("SELECT payload_json FROM github_events LIMIT 1").fetchone()[0])
            route = workflow_routing.effective_route(runtime, repo, event)
            queued = review_store.enqueue(conn, repo, event, route, route["native_review_workers"])

        self.cycle([])

        with fixture_database.connect(self.db) as conn:
            entry = review_store.session(conn, round_id=queued["round_id"])
            self.assertEqual(entry["status"], "canceled")
            self.assertEqual(entry["error"], "trigger_already_processed")
            self.assertIsNone(entry["task_id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        self.cycle([self.event("fresh")])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    def test_known_pr_review_request_uses_requester_trust_and_native_author_gate(self):
        self.cycle([self.event()])
        request = self.event("requested") | {
            "event_type": "review_request",
            "body": "",
            "requested_reviewer": "robert-bot",
            "requester_login": "maintainer",
        }
        self.cycle(
            [
                request,
                request,
                request | {"id": "untrusted", "actor_login": "stranger", "requester_login": "stranger"},
                request | {"id": "wrong-target", "requested_reviewer": "someone-else"},
            ]
        )
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)
        config = yaml.safe_load(self.config_path.read_text())
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([request | {"id": "disabled"}])
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(config))
        self.cycle([request | {"id": "unmatched", "pr_author_login": "other"}])
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

    def test_live_review_request_follows_old_assignment_and_uses_requester(self):
        config = yaml.safe_load(self.config_path.read_text())
        config["routes"]["review-pr"]["multi_agent_review"]["author_allowlist"] = ["robert-bot"]
        self.config_path.write_text(yaml.safe_dump(config))
        assigned = self.event("10", author="robert-bot") | {
            "event_type": "assigned",
            "actor_login": "robert-bot",
            "assignment_actor_login": "maintainer",
            "assigned_to": "robert-bot",
            "body": "",
            "intent": "unclear",
        }
        self.cycle([assigned])
        timeline = [
            {
                "id": 10,
                "event": "assigned",
                "actor": {"login": "maintainer"},
                "assignee": {"login": "robert-bot"},
                "created_at": "2026-10-25T00:00:00Z",
            },
            {
                "id": 11,
                "event": "review_requested",
                "actor": {"login": "maintainer"},
                "requested_reviewer": {"login": "robert-bot"},
                "created_at": "2026-10-25T01:00:00Z",
            },
        ]

        def api(command, **kwargs):
            if command[:3] == ["gh", "search", "issues"]:
                value = []
            elif command[:3] == ["gh", "api", "notifications"]:
                value = [
                    [
                        {
                            "id": "notify-42",
                            "reason": "review_requested",
                            "updated_at": "2026-10-25T01:00:00Z",
                            "repository": {"full_name": "example/backend"},
                            "subject": {
                                "type": "PullRequest",
                                "url": "https://api.github.com/repos/example/backend/pulls/42",
                            },
                        }
                    ]
                ]
            elif command[2] in {"repos/example/backend/pulls/42", "repos/example/backend/issues/42"}:
                value = self.pr("", 42) | {
                    "user": {"login": "robert-bot"},
                    "title": "PR",
                    "body": "",
                    "html_url": "https://github.com/example/backend/pull/42",
                }
            elif command[2] == "repos/example/backend/issues/42/timeline":
                value = timeline
            else:
                raise AssertionError(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        def poll():
            with (
                patch.object(source_lifecycle, "reconcile_remote_source_states", return_value=0),
                patch.object(remediation_requests, "collect_pr_remediation_decisions", return_value=[]),
            ):
                result = run_once.run_once(
                    self.config_path,
                    workflow_path=resource("workflow.yml"),
                    dry_run=True,
                    skip_publish=True,
                    discovery_runner=api,
                )
            self.assertTrue(result["ok"], result)

        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = False
        self.config_path.write_text(yaml.safe_dump(config))
        poll()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 1)
        config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = True
        self.config_path.write_text(yaml.safe_dump(config))
        poll()
        poll()
        with fixture_database.connect(self.db) as conn:
            sessions = conn.execute("SELECT trigger_fingerprint FROM review_sessions").fetchall()
            self.assertEqual({row[0] for row in sessions}, {"assigned:10", "review_request:11"})
        timeline.append(
            {
                "id": 12,
                "event": "review_requested",
                "actor": {"login": "stranger"},
                "requested_reviewer": {"login": "robert-bot"},
                "created_at": "2026-10-25T02:00:00Z",
            }
        )
        poll()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_sessions").fetchone()[0], 2)

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

    def test_synthesis_retry_does_not_rerun_reviewers(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def fail_synthesis(**kwargs):
            if Path(kwargs["prompt_path"]).name.startswith("synthesis-prompt-"):
                return {"ok": False, "safe_error": "synthesis unavailable"}
            return original(**kwargs)

        driver.launcher = fail_synthesis
        with self.assertRaisesRegex(RuntimeError, "synthesis unavailable"):
            driver.run()
        self.coordinator().run()
        counts = Counter((c[0], c[1]) for c in self.calls)
        self.assertEqual(counts[("review", "codex")], 1)
        self.assertEqual(counts[("review", "claude")], 1)

    def test_cancel_fences_late_file_and_publication(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        original = driver.launcher

        def cancel(**kwargs):
            result = original(**kwargs)
            with fixture_database.connect(self.db) as conn:
                conn.execute("UPDATE tasks SET lifecycle='canceled' WHERE task_id=?", (driver.task_id,))
            return result

        driver.launcher = cancel
        with self.assertRaises(review_coordinator.StageStopped):
            driver.run()
        with fixture_database.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM github_actions").fetchone()[0], 0)
            self.assertEqual(review_store.session(conn, task_id=driver.task_id)["status"], "canceled")

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

    def test_disabled_or_unmatched_followup_preserves_legacy_route(self):
        config = validate_config.validate_config(self.config_path, skip_external=True)
        event = {**self.event(), "authorization_status": "accepted_context"}
        self.assertEqual(workflow_routing.effective_route(config, config["repos"][0], event)["route_id"], "review-pr")
        for author, enabled in [("other", True), ("author", False)]:
            config["routes"]["review-pr"]["multi_agent_review"]["enabled"] = enabled
            result = workflow_routing.effective_route(config, config["repos"][0], {**event, "pr_author_login": author})
            self.assertEqual(result["route_id"], "review-comment")
            self.assertNotIn("native_review_workers", result)

    def test_changed_result_after_failed_synthesis_gets_new_sealed_inputs(self):
        self.cycle([self.event()])
        driver = self.coordinator()
        driver.prepare()
        records = driver.review()
        driver.launcher = lambda **kwargs: {"ok": False, "safe_error": "synthesis unavailable"}
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

    @unittest.skipUnless(hasattr(os, "killpg"), "requires process groups")
    def test_exited_model_with_live_child_cannot_register_result(self):
        self.check_lingering_child("success")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires process groups")
    def test_failed_startup_cleans_descendants_before_releasing_capacity(self):
        self.check_lingering_child("failure")

    @unittest.skipUnless(hasattr(os, "killpg"), "requires process groups")
    def test_timeout_with_child_ignoring_termination_requires_attention(self):
        self.check_lingering_child("timeout")

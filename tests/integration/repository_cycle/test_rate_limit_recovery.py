from robert_agent.adapters.processes import attempt as attempt_process
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import dispatch as dispatch_queue
from robert_agent.execution.application import monitor
from robert_agent.work.application import preparation
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class RateLimitRecoveryTests(RepositoryCycleFixture):
    def test_rate_limit_without_progress_automatically_resumes_on_fallback(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._rate_limited_attempt()
        with patch.object(
            attempt_process, "dispatch_status", return_value={"status": "dead"}
        ):
            result = run_once.run_once(
                self.config_path, dry_run=True, skip_external=True
            )
        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn:
            attempts = conn.execute(
                "SELECT attempt_id,status,worktree_path,metadata_json,failure_json"
                " FROM attempts ORDER BY attempt_no"
            ).fetchall()
            self.assertEqual([row[1] for row in attempts], ["failed", "prepared"])
            self.assertEqual(attempts[0][2], attempts[1][2])
            fallback = json.loads(attempts[1][3])["resume"]["model_fallback"]
            self.assertEqual(fallback["model"], "model-fallback")
            self.assertEqual(fallback["reason_code"], "rate_limited")
            self.assertEqual(json.loads(attempts[0][4])["error_class"], "rate_limited")
            worker = {
                "name": "default",
                "default_model": "model-primary",
                "fallback_model": "model-fallback",
            }
            self.assertEqual(
                dispatch_queue._model_for_dispatch(
                    conn,
                    "github:example/backend#123",
                    attempts[1][0],
                    worker,
                    "model-primary",
                ),
                "model-fallback",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT recovery_attempts FROM execution_subjects WHERE subject_id=?",
                    (task_id,),
                ).fetchone()[0],
                1,
            )

    def test_rate_limit_without_usable_fallback_does_not_repeat_primary(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        for fallback, model in (
            (None, "model-primary"),
            ("model-primary", "model-primary"),
            ("model-fallback", "model-fallback"),
        ):
            with self.subTest(fallback=fallback, model=model):
                fixture = RepositoryCycleFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                db_path, _, _ = fixture._rate_limited_attempt(
                    fallback=fallback, model=model, phase=True
                )
                with patch.object(
                    attempt_process, "dispatch_status", return_value={"status": "dead"}
                ):
                    run_once.run_once(
                        fixture.config_path, dry_run=True, skip_external=True
                    )
                with closing(sqlite3.connect(db_path)) as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
                    )
                    failure = json.loads(
                        conn.execute("SELECT failure_json FROM attempts").fetchone()[0]
                    )
                    self.assertEqual(failure["error_class"], "rate_limited")

    def test_immediate_rate_limit_exit_enters_fallback_recovery(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, attempt_id = self._rate_limited_attempt()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE attempts SET status='prepared'")
            target, worktree = dispatch_queue.current_dispatch_target(conn, attempt_id)
        log = self.data_dir / "artifacts" / task_id / f"{attempt_id}.stdout.log"
        worker = {
            "name": "default",
            "command": "unused",
            "agent": "command",
            "default_model": "model-primary",
            "fallback_model": "model-fallback",
            "default_effort": "high",
        }
        with (
            patch.object(
                dispatch_queue, "ensure_dispatch_worktree", return_value=worktree
            ),
            patch.object(
                cycle_worker,
                "dispatch_worker",
                return_value={
                    "ok": False,
                    "status": "failed_dispatch",
                    "pid": 987654,
                    "returncode": 1,
                    "stdout_path": str(log),
                    "safe_error": "worker exited immediately with code 1",
                },
            ),
        ):
            dispatched = dispatch_queue.dispatch_queued(
                db_path,
                ["repo:example/backend"],
                [
                    {
                        "task_info": target,
                        "worktree_result": worktree,
                        "repo_id": "repo:example/backend",
                        "repo": {},
                    }
                ],
                False,
                3,
                workers=[worker],
            )
        self.assertTrue(dispatched[0]["ok"], dispatched)
        with patch.object(
            attempt_process, "dispatch_status", return_value={"status": "dead"}
        ):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2
            )
            metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM attempts ORDER BY attempt_no DESC LIMIT 1"
                ).fetchone()[0]
            )
            self.assertEqual(
                metadata["resume"]["model_fallback"]["model"], "model-fallback"
            )

    def test_fallback_rate_limit_stops_after_one_automatic_switch(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        db_path, task_id, _ = self._rate_limited_attempt()
        with patch.object(
            attempt_process, "dispatch_status", return_value={"status": "dead"}
        ):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            current_id, raw = conn.execute(
                "SELECT attempt_id,metadata_json FROM attempts ORDER BY attempt_no DESC LIMIT 1"
            ).fetchone()
            metadata = json.loads(raw)
            metadata["dispatch"] = {
                **self._recorded_identity(987654),
                "worker_name": "default",
                "model": "model-fallback",
                "fallback_model": "model-fallback",
            }
            conn.execute(
                "UPDATE attempts SET status='running',metadata_json = json_set(?, '$.result_contract', 'compact-v1') WHERE attempt_id=?",
                (json.dumps(metadata), current_id),
            )
            log = self.data_dir / "artifacts" / task_id / f"{current_id}.stdout.log"
            log.write_text(
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {"message": "429 Too Many Requests"},
                    }
                )
            )
            conn.execute(
                "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)"
                " VALUES ('fallback-log',?,?,'worker_stdout',?,?)",
                (task_id, current_id, str(log), datetime.now(timezone.utc).isoformat()),
            )
        with patch.object(
            attempt_process, "dispatch_status", return_value={"status": "dead"}
        ):
            run_once.run_once(self.config_path, dry_run=True, skip_external=True)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2
            )
            self.assertEqual(
                conn.execute("SELECT lifecycle FROM tasks").fetchone()[0], "failed"
            )
            failure = json.loads(
                conn.execute(
                    "SELECT failure_json FROM attempts WHERE attempt_id=?",
                    (current_id,),
                ).fetchone()[0]
            )
            self.assertEqual(failure["error_class"], "rate_limited")

    def test_rate_limit_does_not_bypass_recovery_quota_or_task_cancellation(self):
        from unittest.mock import patch
        from robert_agent.runtime import cycle as run_once

        for state in ("quota_exhausted", "canceled", "alive"):
            with self.subTest(state=state):
                fixture = RepositoryCycleFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                db_path, task_id, _ = fixture._rate_limited_attempt()
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    if state == "quota_exhausted":
                        conn.execute(
                            "UPDATE execution_subjects SET recovery_attempts=1 WHERE subject_id=?",
                            (task_id,),
                        )
                    elif state == "canceled":
                        conn.execute("UPDATE tasks SET lifecycle='canceled'")
                        conn.execute(
                            "UPDATE execution_subjects SET binding_status='terminal' WHERE subject_id=?",
                            (task_id,),
                        )
                process_status = "alive_same" if state == "alive" else "dead"
                with patch.object(
                    attempt_process,
                    "dispatch_status",
                    return_value={"status": process_status},
                ):
                    run_once.run_once(
                        fixture.config_path, dry_run=True, skip_external=True
                    )
                with closing(sqlite3.connect(db_path)) as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
                    )

    def test_rate_limit_after_recorded_result_does_not_resume(self):
        from unittest.mock import patch
        from robert_agent.delivery.application import submission as worker_result

        db_path, task_id, attempt_id = self._rate_limited_attempt()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET expected_output='local_result'")
            conn.execute(
                "UPDATE route_decisions SET expected_output='local_result', route_id='local-result'"
            )
        payload = worker_result.build_result(
            task_id,
            attempt_id,
            "local_result",
            planned_github_actions=[],
            consumed_event_fingerprints=["comment:1"],
            verification=[],
            handoff="recorded",
            used_skills=[],
        )
        self.assertTrue(worker_result.record_result(db_path, payload)["ok"])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            with patch.object(
                attempt_process, "dispatch_status", return_value={"status": "dead"}
            ):
                monitor.supervise_running_attempts(
                    conn,
                    "repo:example/backend",
                    {},
                    datetime.now(timezone.utc).isoformat(),
                    data_dir=self.data_dir,
                    db_path=db_path,
                    repo={"default_base_branch": "master"},
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT status FROM attempts").fetchone()[0], "completed"
            )

    def test_real_worker_rate_limit_switches_model_and_records_result(self):
        import sys
        from unittest.mock import patch

        db_path, task_id, attempt_id = self._interrupted_attempt(phase=False)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET expected_output='local_result'")
            conn.execute(
                "UPDATE route_decisions SET expected_output='local_result', route_id='local-result'"
            )
        binary = self.root / "limited-worker.py"
        binary.write_text("""import json,sys,time
from pathlib import Path
from robert_agent.delivery.application.submission import build_result,record_result
model=sys.argv[sys.argv.index('--model')+1]
db,task=sys.argv[1:3]
prompt=sys.stdin.read()
path=Path(sys.argv[sys.argv.index('--add-dir')+1])
with (path/'models.jsonl').open('a') as stream:stream.write(json.dumps(model)+'\\n')
if model=='model-primary':
    print(json.dumps({'type':'turn.failed','error':{'message':'429 Too Many Requests'}}),flush=True)
    sys.exit(1)
attempt=next(path.glob('*.recovery.json')).name.removesuffix('.recovery.json')
payload=build_result(task,attempt,'local_result',planned_github_actions=[],
    consumed_event_fingerprints=['comment:1'],verification=[],handoff='fallback completed',used_skills=[])
assert record_result(db,payload)['ok']
print(json.dumps({'type':'turn.completed'}),flush=True)
time.sleep(.3)
""")
        worker = {
            "name": "default",
            "command": [sys.executable, str(binary), str(db_path), task_id],
            "agent": "tcodex",
            "default_model": "model-primary",
            "fallback_model": "model-fallback",
            "default_effort": "high",
            "environment_allowlist": ["PYTHONPATH"],
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET status='prepared',metadata_json = json_set('{}', '$.result_contract', 'compact-v1')"
            )
        for _ in range(2):
            with closing(sqlite3.connect(db_path)) as conn:
                current = conn.execute(
                    "SELECT attempt_id FROM attempts WHERE status='prepared' ORDER BY attempt_no DESC LIMIT 1"
                ).fetchone()[0]
                target, worktree = dispatch_queue.current_dispatch_target(conn, current)
            Path(worktree["worktree_path"]).mkdir(parents=True, exist_ok=True)
            with patch.object(
                dispatch_queue, "ensure_dispatch_worktree", return_value=worktree
            ):
                launched = dispatch_queue.dispatch_queued(
                    db_path,
                    ["repo:example/backend"],
                    [
                        {
                            "task_info": target,
                            "worktree_result": worktree,
                            "repo_id": "repo:example/backend",
                            "repo": {},
                        }
                    ],
                    False,
                    3,
                    workers=[worker],
                )
            self.assertTrue(launched[0]["ok"], launched)
            if launched[0].get("pid") in cycle_worker._BACKGROUND_PROCESSES:
                cycle_worker._BACKGROUND_PROCESSES[launched[0]["pid"]].wait(timeout=5)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                monitor.supervise_running_attempts(
                    conn,
                    "repo:example/backend",
                    {},
                    datetime.now(timezone.utc).isoformat(),
                    data_dir=self.data_dir,
                    db_path=db_path,
                    repo={"default_base_branch": "master"},
                    resume_attempt=preparation.create_resume_attempt_and_prompt,
                )
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(
                [
                    row[0]
                    for row in conn.execute(
                        "SELECT status FROM attempts ORDER BY attempt_no"
                    )
                ],
                ["failed", "completed"],
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0], 1
            )
        models = self.data_dir / "artifacts" / task_id / "models.jsonl"
        self.assertEqual(
            [json.loads(line) for line in models.read_text().splitlines()],
            ["model-primary", "model-fallback"],
        )

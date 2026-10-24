from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from robert_agent import storage, validate_config


class SQLiteRuntimeTests(unittest.TestCase):
    def test_active_and_shadow_configs_do_not_silently_fall_back(self):
        for mode in ("active", "shadow"):
            with self.assertRaisesRegex(ValueError, "migrate dbos"):
                validate_config._normalize_workflow_engine(
                    {"workflow_engine": {"mode": mode}},
                    environ={
                        "ROBERT_DBOS_SYSTEM_DATABASE_URL": "postgresql://local/old"
                    },
                )

    def test_initialization_has_neutral_controls_without_dbos_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            storage.init_database(path)
            with closing(sqlite3.connect(path)) as conn:
                fields = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(execution_subjects)")
                }
                self.assertIn("recovery_attempts", fields)
                self.assertNotIn("owner_engine", fields)

    def test_direct_entrypoints_refuse_unmigrated_dbos_subject(self):
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import publish, run_once, daemon
        from tests import legacy_workflow_store as workflow_store
        from robert_agent.worker import result

        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db, task, attempt = fixture._seed_active_pr_task(999)
        with closing(sqlite3.connect(db)) as conn, conn:
            workflow_store.bind_subject(
                conn,
                "task",
                task,
                "human_request",
                "dbos",
                "now",
                definition_version="v2",
            )
        with self.assertRaisesRegex(RuntimeError, "offline migration"):
            publish.publish_ready_actions(db)
        with self.assertRaisesRegex(RuntimeError, "offline migration"):
            run_once.run_once(fixture.config_path, dry_run=False, skip_external=True)
        _, error = daemon._load_context(fixture.config_path)
        self.assertEqual(error["status"], "migration_required")
        payload = result.build_result(
            task, attempt, "comment_analysis", [], [], "done", planned_github_actions=[]
        )
        with self.assertRaisesRegex(RuntimeError, "offline migration"):
            result.record_result(db, payload)
        from robert_agent import web
        with self.assertRaisesRegex(RuntimeError, "offline migration"):
            web.build_control_context_from_config(
                fixture.config_path, host="127.0.0.1", port=8765, operator_identity="test"
            )
        from contextlib import redirect_stdout
        import io
        from robert_agent.cli.main import main
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["run", "once", "--config", str(fixture.config_path), "--dry-run", "--skip-external"])
        self.assertNotEqual(code, 0)
        self.assertIn("migration_required", output.getvalue())
        self.assertIn("migrate dbos", output.getvalue())

    def test_native_dispatch_crash_fence_prevents_second_spawn(self):
        from unittest.mock import patch
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import run_once, dispatch

        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        response = run_once.run_once(
            fixture.config_path,
            fixture_path=fixture.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(response["ok"], response)
        config = validate_config.validate_config(
            fixture.config_path, skip_external=True
        )
        db = fixture.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            task, attempt, stream, route = conn.execute(
                "SELECT t.task_id,a.attempt_id,t.workstream_id,t.route_id FROM tasks t JOIN attempts a ON a.task_id=t.task_id"
            ).fetchone()
        queue = [
            {
                "task_info": {
                    "task_id": task,
                    "attempt_id": attempt,
                    "workstream_id": stream,
                    "route_id": route,
                },
                "worktree_result": None,
                "repo_id": "repo:example/backend",
                "repo": config["repos"][0],
            }
        ]
        kwargs = {
            "workers": config["workers"],
            "default_worker": config["default_worker"],
            "config_result": config,
        }
        with (
            patch.object(run_once, "_ensure_dispatch_worktree", return_value={}),
            patch.object(
                dispatch, "dispatch_worker", side_effect=SystemExit("crash")
            ) as spawn,
        ):
            with self.assertRaises(SystemExit):
                run_once._dispatch_queued(
                    db, ["repo:example/backend"], queue, False, 3, **kwargs
                )
            self.assertEqual(spawn.call_count, 1)

        with (
            patch.object(run_once, "_ensure_dispatch_worktree", return_value={}),
            patch.object(dispatch, "dispatch_worker") as spawn,
        ):
            run_once._dispatch_queued(
                db, ["repo:example/backend"], queue, False, 3, **kwargs
            )
            spawn.assert_not_called()
        with closing(sqlite3.connect(db)) as conn:
            state, metadata = conn.execute(
                "SELECT binding_status,metadata_json FROM execution_subjects WHERE subject_id=?",
                (task,),
            ).fetchone()
            self.assertEqual(state, "attention")
            self.assertIn("dispatch_outcome_unknown", metadata)

    def _prepared_queue(self, count=1):
        import json
        from tests import test_run_once
        from robert_agent import run_once

        fixture = test_run_once.RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        event = json.loads(fixture.fixture_path.read_text())["events"][0]
        fixture.fixture_path.write_text(json.dumps({"events": [
            {**event, "id": f"acceptance-{number}", "number": 800 + number}
            for number in range(count)
        ]}))
        response = run_once.run_once(fixture.config_path, fixture_path=fixture.fixture_path, dry_run=True, skip_external=True)
        self.assertTrue(response["ok"], response)
        config = validate_config.validate_config(fixture.config_path, skip_external=True)
        db = fixture.data_dir / "dd.sqlite3"
        queue = []
        with closing(sqlite3.connect(db)) as conn, conn:
            for (attempt,) in conn.execute("SELECT attempt_id FROM attempts").fetchall():
                task, worktree = run_once._current_dispatch_target(conn, attempt)
                queue.append({"task_info": task, "worktree_result": worktree, "repo_id": "repo:example/backend", "repo": config["repos"][0]})
        self.assertEqual(len(queue), count)
        return db, queue, {"workers": config["workers"], "default_worker": config["default_worker"], "config_result": config}

    def test_different_tasks_cannot_race_past_capacity(self):
        from unittest.mock import patch
        from robert_agent import run_once, dispatch

        db, queue, kwargs = self._prepared_queue(2)
        entered = False
        def competing_dispatch(*_args):
            nonlocal entered
            if not entered:
                entered = True
                run_once._dispatch_queued(db, ["repo:example/backend"], queue[1:], False, 1, **kwargs)
            return {}
        with patch.object(run_once, "_ensure_dispatch_worktree", side_effect=competing_dispatch), patch.object(dispatch, "dispatch_worker", return_value={"ok": True, "status": "running", "pid": 123456}) as spawn:
            run_once._dispatch_queued(db, ["repo:example/backend"], queue[:1], False, 1, **kwargs)
            run_once._dispatch_queued(db, ["repo:example/backend"], queue[1:], False, 1, **kwargs)
        self.assertEqual(spawn.call_count, 1)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts WHERE status='running'").fetchone()[0], 1)

    def test_missing_episode_stops_once_and_is_not_runnable(self):
        import json
        from robert_agent import run_once, loop_engine

        db, queue, _kwargs = self._prepared_queue()
        task = queue[0]["task_info"]["task_id"]
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE tasks SET task_kind='ci_remediation',metadata_json='{}' WHERE task_id=?", (task,))
            conn.execute("DELETE FROM execution_subjects WHERE subject_id=?", (task,))
            for _ in range(2):
                self.assertFalse(run_once._execution_task_active(conn, task))
            metadata = json.loads(conn.execute("SELECT metadata_json FROM tasks WHERE task_id=?", (task,)).fetchone()[0])
            self.assertEqual(metadata["execution_attention"]["reason_code"], "remediation_episode_missing")
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications WHERE notification_type='execution_attention_required'").fetchone()[0], 1)
            self.assertFalse(loop_engine.has_runnable_local_work(conn))

    def test_fact_replay_never_replaces_newer_evidence(self):
        import json
        from tests.test_workflow_events import WorkflowEventApplicationTests, _fact, NOW
        from robert_agent import execution_operations, execution_store

        fixture = WorkflowEventApplicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            execution_store.ensure_subject(conn, "task", "task-1", "human_request", NOW)
            for fact in (
                _fact(fact_id="old"),
                _fact(fact_id="new"),
                _fact(fact_id="old"),
            ):
                result = execution_operations.apply_classified_fact(
                    conn, "task", "task-1", fact, now=NOW
                )
            self.assertTrue(result["duplicate"])
            self.assertEqual(
                json.loads(
                    execution_store.get_binding(conn, "task", "task-1")["metadata_json"]
                )["latest_fact"]["fact_id"],
                "new",
            )

    def test_concurrent_dispatch_cannot_reuse_stale_prepared_target(self):
        from unittest.mock import patch
        from tests.test_run_once import RunOnceDryRunTests
        from robert_agent import run_once, dispatch

        fixture = RunOnceDryRunTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        response = run_once.run_once(
            fixture.config_path,
            fixture_path=fixture.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(response["ok"], response)
        config = validate_config.validate_config(
            fixture.config_path, skip_external=True
        )
        db = fixture.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            task, attempt, stream, route = conn.execute(
                "SELECT t.task_id,a.attempt_id,t.workstream_id,t.route_id FROM tasks t JOIN attempts a ON a.task_id=t.task_id"
            ).fetchone()
        queue = [
            {
                "task_info": {
                    "task_id": task,
                    "attempt_id": attempt,
                    "workstream_id": stream,
                    "route_id": route,
                },
                "worktree_result": None,
                "repo_id": "repo:example/backend",
                "repo": config["repos"][0],
            }
        ]
        kwargs = {
            "workers": config["workers"],
            "default_worker": config["default_worker"],
            "config_result": config,
        }
        entered = False

        def ensure(*args, **kw):
            nonlocal entered
            if not entered:
                entered = True
                run_once._dispatch_queued(
                    db, ["repo:example/backend"], queue, False, 3, **kwargs
                )
            return {}

        with (
            patch.object(run_once, "_ensure_dispatch_worktree", side_effect=ensure),
            patch.object(
                dispatch,
                "dispatch_worker",
                return_value={"ok": True, "status": "running", "pid": 123456},
            ) as spawn,
        ):
            run_once._dispatch_queued(
                db, ["repo:example/backend"], queue, False, 3, **kwargs
            )
            self.assertEqual(spawn.call_count, 1)

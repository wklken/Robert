from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class LoopEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "repo"
        self.repo_root.mkdir()
        (self.repo_root / ".git").mkdir()
        self.worktree_root = self.repo_root / ".worktrees"
        self.worktree_root.mkdir()
        self.data_dir = self.root / "data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""data_dir: {self.data_dir}
database: dd.sqlite3
max_concurrency: 3
stale_after_minutes: 20
hard_timeout_minutes: 90
default_max_retries: 0
repos:
  - full_name: example/repo
    github_account: robot
    trusted_actors:
      - wklken
    default_base_branch: master
    repo_root: {self.repo_root}
    worktree_root: {self.worktree_root}
""",
            encoding="utf-8",
        )

    def test_loop_stops_without_runnable_work(self):
        from robert_agent import loop_engine
        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            runner=self._unexpected_runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 0)
        self.assertEqual(result["stop_reason"], "no_runnable_work")
        self.assertTrue(Path(result["latest_loop_path"]).is_file())

    def test_loop_ignores_local_work_outside_configured_repos(self):
        from robert_agent import loop_engine
        db_path = loop_engine.load_config_db_path(self.config_path, skip_external=True)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO repos(
                  repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root
                )
                VALUES ('repo:other', 'example/other', 'robot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO wakeups(
                  wakeup_id, repo_id, reason, dedupe_key, status,
                  not_before_at, created_at, updated_at
                )
                VALUES ('wakeup-other', 'repo:other', 'manual_operator_request', 'manual-other',
                        'pending', ?, ?, ?)
                """,
                (now, now, now),
            )

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            runner=self._unexpected_runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 0)
        self.assertEqual(result["stop_reason"], "no_runnable_work")

    def test_loop_stops_when_cycle_makes_no_progress(self):
        from robert_agent import loop_engine
        db_path = self._init_runnable_db()

        def runner(*_args, **_kwargs):
            return {"ok": True, "status": "completed", "db_path": str(db_path)}

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            runner=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["stop_reason"], "no_progress")
        self.assertFalse(result["cycle_results"][0]["progress"]["changed"])

    def test_loop_ignores_agent_run_count_when_detecting_progress(self):
        from robert_agent import loop_engine
        db_path = self._init_runnable_db()

        def runner(*_args, **_kwargs):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO agent_runs(run_id, status, started_at, dry_run)
                    VALUES ('run-no-business-progress', 'completed', ?, 1)
                    """,
                    (datetime.now(timezone.utc).isoformat(),),
                )
            return {"ok": True, "status": "completed", "run_id": "run-no-business-progress"}

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            runner=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["stop_reason"], "no_progress")
        self.assertFalse(result["cycle_results"][0]["progress"]["changed"])

    def test_loop_stops_at_max_cycles_while_progress_continues(self):
        from robert_agent import loop_engine
        db_path = self._init_runnable_db()
        calls = []

        def runner(*_args, **_kwargs):
            calls.append(len(calls) + 1)
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO workstreams(
                      workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                    )
                    VALUES (?, 'repo-1', 'active', ?, ?, ?)
                    """,
                    (
                        f"workstream-{len(calls)}",
                        f"task-{len(calls)}",
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                    VALUES (?, ?, 'running', 'P1', ?, ?)
                    """,
                    (
                        f"task-{len(calls)}",
                        f"workstream-{len(calls)}",
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            return {"ok": True, "status": "completed", "run_id": f"run-{len(calls)}"}

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            max_cycles=2,
            runner=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 2)
        self.assertEqual(result["stop_reason"], "max_cycles")
        self.assertEqual(calls, [1, 2])

    def test_loop_runs_when_attempt_needs_supervision(self):
        from robert_agent import loop_engine
        db_path = loop_engine.load_config_db_path(self.config_path, skip_external=True)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO repos(
                  repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root
                )
                VALUES ('repo-1', 'example/repo', 'robot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('workstream-supervise', 'repo-1', 'active', 'task-supervise', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                VALUES ('task-supervise', 'workstream-supervise', 'running', 'P1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, started_at, heartbeat_at
                )
                VALUES ('attempt-supervise', 'task-supervise', 1, 'stale', ?, ?)
                """,
                (now, now),
            )

        calls = []

        def runner(*_args, **_kwargs):
            calls.append(True)
            return {"ok": True, "status": "completed", "run_id": "run-supervise"}

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            runner=runner,
        )

        self.assertEqual(calls, [True])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["stop_reason"], "no_progress")

    def test_loop_stops_when_run_once_fails(self):
        from robert_agent import loop_engine
        self._init_runnable_db()

        def runner(*_args, **_kwargs):
            return {"ok": False, "status": "failed_dispatch", "safe_error": "worker failed"}

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            runner=runner,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["stop_reason"], "run_once_failed")

    def test_loop_passes_skip_publish_to_run_once(self):
        from robert_agent import loop_engine
        self._init_runnable_db()
        runner_kwargs = []

        def runner(*_args, **kwargs):
            runner_kwargs.append(kwargs)
            return {"ok": False, "status": "failed_dispatch", "safe_error": "worker failed"}

        result = loop_engine.run_loop(
            self.config_path,
            dry_run=True,
            skip_external=True,
            skip_publish=True,
            runner=runner,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(runner_kwargs[0]["skip_publish"], True)

    def _init_runnable_db(self):
        from robert_agent import loop_engine
        db_path = loop_engine.load_config_db_path(self.config_path, skip_external=True)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO repos(
                  repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root
                )
                VALUES ('repo-1', 'example/repo', 'robot', 'master', '/repo', '/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO wakeups(
                  wakeup_id, repo_id, reason, dedupe_key, status,
                  not_before_at, created_at, updated_at
                )
                VALUES ('wakeup-1', 'repo-1', 'manual_operator_request', 'manual-1',
                        'pending', ?, ?, ?)
                """,
                (now, now, now),
            )
        return db_path

    def _unexpected_runner(self, *_args, **_kwargs):
        raise AssertionError("runner should not be called without runnable work")


if __name__ == "__main__":
    unittest.main()

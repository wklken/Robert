from contextlib import closing
import sqlite3
from tests.support.temp_workspace import TemporaryWorkspace
from tests.support.compact_result import CompactResultFixture


class LoopRunnableScopeTests(TemporaryWorkspace):
    def test_loop_engine_local_work_accepts_repo_id_list(self):
        from robert_agent.runtime import loop as loop_engine
        from robert_agent.persistence.schema import database as storage

        data_dir = self.root / "data"
        repo_root = self.root / "repo-a"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_root = repo_root / ".worktrees"
        worktree_root.mkdir()
        db_path = data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo:a', 'Org/a', 'robot', 'main', ?, ?)",
                (str(repo_root), str(worktree_root)),
            )
            conn.execute(
                "INSERT INTO wakeups(wakeup_id, repo_id, reason, dedupe_key, status, not_before_at, created_at, updated_at) VALUES ('wakeup-a', 'repo:a', 'manual_operator_request', 'manual-a', 'pending', ?, ?, ?)",
                (now, now, now),
            )
            self.assertTrue(loop_engine.has_runnable_local_work(conn, repo_ids=["repo:a"]))
            self.assertFalse(loop_engine.has_runnable_local_work(conn, repo_ids=["repo:b"]))


class TaskWakeupReadinessTests(CompactResultFixture):
    def test_terminal_task_ordinary_wakeup_does_not_make_local_work_runnable(self):
        from robert_agent.execution.application import readiness
        from robert_agent.work import wakeup

        db, task_id, attempt_id, _, _ = self.prepare("comment")
        now = "2026-11-21T00:00:00+00:00"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE wakeups SET status='expired'")
            conn.execute("UPDATE attempts SET status='failed' WHERE attempt_id=?", (attempt_id,))
            repo_id = conn.execute("SELECT repo_id FROM workstreams").fetchone()[0]
            wakeup.request_wakeup(
                conn,
                repo_id=repo_id,
                task_id=task_id,
                reason="manual_operator_request",
                dedupe_key="old-operator-request",
                now=now,
            )
            for lifecycle in ("canceled", "ignored", "completed", "failed"):
                with self.subTest(lifecycle=lifecycle):
                    conn.execute("UPDATE tasks SET lifecycle=? WHERE task_id=?", (lifecycle, task_id))
                    self.assertFalse(
                        readiness.has_local_work(
                            conn,
                            [repo_id],
                            config_result={},
                            now=now,
                            include_publication=False,
                        )
                    )

    def test_terminal_retrigger_and_publication_retry_keep_their_own_consumers(self):
        from robert_agent.persistence.queries import scheduling
        from robert_agent.work import wakeup

        db, task_id, _, _, _ = self.prepare("comment")
        now = "2026-11-21T00:00:00+00:00"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE wakeups SET status='expired'")
            repo_id = conn.execute("SELECT repo_id FROM workstreams").fetchone()[0]
            for lifecycle, kind in (
                ("failed", "github_retrigger"),
                ("canceled", "github_retrigger"),
                ("completed", "publication_retry"),
            ):
                with self.subTest(lifecycle=lifecycle, kind=kind):
                    conn.execute("UPDATE wakeups SET status='expired'")
                    conn.execute("UPDATE tasks SET lifecycle=? WHERE task_id=?", (lifecycle, task_id))
                    wakeup.request_wakeup(
                        conn,
                        repo_id=repo_id,
                        task_id=task_id,
                        reason="manual_operator_request",
                        dedupe_key=f"{lifecycle}-{kind}",
                        metadata={"kind": kind},
                        now=now,
                    )
                    self.assertTrue(scheduling.has_pending_local_wakeup(conn, [repo_id], [], now))

    def test_live_task_wakeup_keeps_existing_scope_and_attention_guards(self):
        from robert_agent.persistence.queries import scheduling
        from robert_agent.work import wakeup

        db, task_id, _, _, _ = self.prepare("comment")
        now = "2026-11-21T00:00:00+00:00"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE wakeups SET status='expired'")
            repo_id = conn.execute("SELECT repo_id FROM workstreams").fetchone()[0]
            wakeup.request_wakeup(
                conn,
                repo_id=repo_id,
                task_id=task_id,
                reason="manual_operator_request",
                dedupe_key="live-request",
                now=now,
            )
            self.assertTrue(scheduling.has_pending_local_wakeup(conn, [repo_id], [], now))
            self.assertFalse(scheduling.has_pending_local_wakeup(conn, ["other-repo"], [], now))
            conn.execute(
                "UPDATE tasks SET metadata_json=json_set(metadata_json,'$.execution_attention',json('{}')) WHERE task_id=?",
                (task_id,),
            )
            self.assertFalse(scheduling.has_pending_local_wakeup(conn, [repo_id], [], now))

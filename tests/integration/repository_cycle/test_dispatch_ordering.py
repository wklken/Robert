from contextlib import closing
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from tests.support import PACKAGE_ROOT
from robert_agent.adapters.processes import worker as cycle_worker
from tests.support.repository_cycle import (
    RepositoryCycleFixture, _verification_evidence,
)


class DispatchOrderingTests(RepositoryCycleFixture):
    def test_failed_trigger_event_is_not_recreated_without_new_event(self):
        from robert_agent.runtime import cycle as run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            workstream_id = conn.execute("SELECT workstream_id FROM tasks").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL, updated_at = ?
                WHERE workstream_id = ?
                """,
                (now, workstream_id),
            )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        self.assertEqual(second["prompt_paths"], [])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            trigger_count = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE relationship = 'trigger'"
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(trigger_count, 1)

    def test_live_run_prioritizes_existing_pr_updates_over_older_prepared_work(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "repo:example/backend",
                    "example/backend",
                    "robert-bot",
                    "master",
                    str(self.repo_root),
                    str(self.worktree_root),
                ),
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend#2878', 'repo:example/backend', 'github:example/backend#2878', 'issue', 2878, 'older issue', 'open', 'wklken')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend#2878', 'repo:example/backend', 'source:github:example/backend#2878', 'active', 'task-older', ?, ?)
                """,
                (old, old),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES ('task-older', 'github:example/backend#2878', 'queued', 'P1', 'comment-analysis', 'comment_analysis', ?, ?)
                """,
                (old, old),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, metadata_json)
                VALUES ('attempt-older', 'task-older', 1, 'prepared', ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (old, old),
            )
            older_prompt = self.data_dir / "artifacts" / "task-older" / "prompt.md"
            older_prompt.parent.mkdir(parents=True, exist_ok=True)
            older_prompt.write_text("older prompt", encoding="utf-8")
            conn.execute(
                """
                INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at)
                VALUES ('artifact-older', 'task-older', 'attempt-older', 'prompt', ?, ?)
                """,
                (str(older_prompt), old),
            )

            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend!2886', 'repo:example/backend', 'github:example/backend!2886', 'pull_request', 2886, 'fix(operator): improve release log context', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend!2886', 'repo:example/backend', 'source:github:example/backend!2886', 'github:example/backend#2884', 'active', 'task-pr', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES ('task-pr', 'github:example/backend!2886', 'queued', 'P1', 'update-existing-pr', 'update_existing_pr', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at, metadata_json)
                VALUES ('attempt-pr', 'task-pr', 1, 'prepared', ?, 'codex/issue-2884-fix-operator', ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (str(self.worktree_root / "issue-2884-fix-operator"), now, now),
            )
            pr_prompt = self.data_dir / "artifacts" / "task-pr" / "prompt.md"
            pr_prompt.parent.mkdir(parents=True, exist_ok=True)
            pr_prompt.write_text("pr prompt", encoding="utf-8")
            conn.execute(
                """
                INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at)
                VALUES ('artifact-pr', 'task-pr', 'attempt-pr', 'prompt', ?, ?)
                """,
                (str(pr_prompt), now),
            )

            for task, stream, source in [('task-pr', 'github:example/backend!2886', 'source:github:example/backend!2886'),
                                         ('task-older', 'github:example/backend#2878', 'source:github:example/backend#2878')]:
                event = {'event_type': 'comment', 'event_fingerprint': task, 'actor_login': 'wklken',
                         'body': '@robert-bot analyze this', 'workstream_id': stream,
                         'source_key': stream, 'source_type': 'issue', 'number': 2878}
                conn.execute("INSERT INTO github_events(event_id,repo_id,source_id,event_fingerprint,event_type,payload_json,event_at) VALUES(?,'repo:example/backend',?,?,'comment',?,?)",
                             (task, source, task, json.dumps(event), now))
                conn.execute("INSERT INTO task_events(task_id,event_id,relationship,created_at) VALUES(?,?,'trigger',?)", (task, task, now))

        calls = []
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append((kwargs["task_id"], kwargs["attempt_id"], kwargs["prompt_path"]))
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 321,
                "command": ["cbc", "-p"],
            }

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls[0][0], "task-pr")
        self.assertEqual(calls[0][1], "attempt-pr")

    def test_max_concurrency_blocks_new_dispatch_but_keeps_prepared_task(self):
        from robert_agent.runtime import cycle as run_once
        config_text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            config_text.replace("max_concurrency: 3", "max_concurrency: 1"),
            encoding="utf-8",
        )
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        now = datetime.now(timezone.utc).isoformat()
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("UPDATE tasks SET lifecycle = 'running'")
            conn.execute(
                "UPDATE attempts SET status = 'running', started_at = ?, heartbeat_at = ?",
                (now, now),
            )
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-4",
                            "number": 124,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls = []
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 123, "command": ["cbc"]}

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            statuses = conn.execute(
                "SELECT status FROM attempts ORDER BY started_at"
            ).fetchall()
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(statuses, [("running",), ("prepared",)])
        self.assertEqual(task_count, 2)

    def test_child_task_dispatch_respects_live_capacity(self):
        from robert_agent.delivery.application import submission as result
        from robert_agent.runtime import cycle as run_once
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "max_concurrency: 3", "max_concurrency: 1"
            ),
            encoding="utf-8",
        )
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            repo_id = conn.execute("SELECT repo_id FROM repos").fetchone()[0]
            parent_task_id, parent_attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "MEMBER",
                            "body": "@robert-bot another point",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        followup = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(followup["ok"], followup)
        record = result.record_result(
            db_path,
            {
                "task_id": parent_task_id,
                "attempt_id": parent_attempt_id,
                "output_type": "new_pr",
                "submission": {
                    "planned_github_actions": self._new_pr_actions_for_attempt(
                        db_path,
                        parent_task_id,
                    ),
                    "consumed_event_fingerprints": ["comment:comment-1"],
                    "verification": [_verification_evidence()],
                    "handoff": "done",
                    "used_skills": ["fast-small-pr"],
                },
            },
        )
        self.assertTrue(record["ok"], record)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES ('ws-busy', ?, 'active', 'task-busy', ?, ?)
                """,
                (repo_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, created_at, updated_at)
                VALUES ('task-busy', 'ws-busy', 'running', 'P1', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, metadata_json)
                VALUES ('attempt-busy', 'task-busy', 1, 'running', ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (now, now),
            )
        calls = []
        original_dispatch = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 456, "command": ["cbc"]}

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            audited = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            cycle_worker.dispatch_worker = original_dispatch

        self.assertTrue(audited["ok"], audited)
        self.assertEqual(calls, [])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            child = conn.execute(
                """
                SELECT t.parent_task_id, a.status
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                WHERE t.parent_task_id = ?
                """,
                (parent_task_id,),
            ).fetchone()
        self.assertEqual(child, (parent_task_id, "prepared"))

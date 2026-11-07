from contextlib import closing
import json
import sqlite3
from pathlib import Path
from tests.support import PACKAGE_ROOT
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import dispatch as dispatch_queue
from tests.support.repository_cycle import (
    RepositoryCycleFixture,
)


class TaskPreparationTests(RepositoryCycleFixture):
    def test_dry_run_fixture_creates_control_plane_rows_and_prompt_artifact(self):
        from robert_agent.runtime import cycle as run_once

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["publish_result"]["status"], "dry_run")
        db_path = self.data_dir / "dd.sqlite3"
        self.assertTrue(db_path.exists())
        self.assertFalse((self.data_dir / "dispatcher.sqlite3").exists())
        with closing(sqlite3.connect(db_path)) as conn, conn:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in [
                    "repos",
                    "github_sources",
                    "github_events",
                    "workstreams",
                    "tasks",
                    "attempts",
                    "artifacts",
                    "worker_results",
                    "agent_runs",
                ]
            }
            route = conn.execute("SELECT expected_output, recommended_skills_json FROM route_decisions").fetchone()

            source = conn.execute("SELECT html_url, state, source_updated_at FROM github_sources").fetchone()
            attempt = conn.execute("SELECT status, metadata_json, worktree_path, branch_name FROM attempts").fetchone()
            artifacts = dict(conn.execute("SELECT artifact_type, path FROM artifacts").fetchall())
        self.assertEqual(counts["repos"], 1)
        self.assertEqual(counts["github_sources"], 1)
        self.assertEqual(counts["github_events"], 1)
        self.assertEqual(counts["workstreams"], 1)
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual(counts["attempts"], 1)
        self.assertEqual(counts["artifacts"], 3)
        self.assertEqual(counts["worker_results"], 0)
        self.assertEqual(counts["agent_runs"], 1)
        self.assertEqual(
            source[0],
            "https://github.com/example/backend/issues/123",
        )
        self.assertEqual(source[1], "open")
        self.assertEqual(source[2], "2026-06-16T00:00:00Z")
        self.assertEqual(route[0], "new_pr")
        self.assertEqual(
            json.loads(route[1]),
            [
                "fast-small-pr",
                "fast-code-path",
                "fast-add-tests",
                "fast-test-fix",
                "fast-preflight",
            ],
        )
        self.assertEqual(attempt[0], "prepared")
        self.assertIn("dispatch", attempt[1])
        self.assertIn("codex__dd-123", attempt[2])
        self.assertEqual(attempt[3], "codex/dd-123-task")
        self.assertIn(attempt[2], attempt[1])
        prompt_path = Path(result["prompt_paths"][0])
        self.assertTrue(prompt_path.exists())
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Robert generates the issue link", prompt)
        self.assertIn("allowed_github_actions", prompt)
        self.assertIn("worktree_path:", prompt)
        self.assertIn("target_base_branch: master", prompt)
        self.assertIn(str(PACKAGE_ROOT / "worker/result.py"), prompt)
        self.assertIn(str(db_path), prompt)
        self.assertIn("github-context.json", prompt)
        self.assertIn("github-context.md", prompt)
        self.assertIn("@robert-bot please fix this bug", prompt)
        context_json = Path(artifacts["github_context_json"])
        context_md = Path(artifacts["github_context_md"])
        self.assertTrue(context_json.exists())
        self.assertTrue(context_md.exists())
        context_payload = json.loads(context_json.read_text(encoding="utf-8"))
        self.assertEqual(
            context_payload["events"][0]["body"],
            "@robert-bot please fix this bug",
        )
        self.assertIn(
            "@robert-bot please fix this bug",
            context_md.read_text(encoding="utf-8"),
        )

    def test_web_work_item_materializes_without_fake_github_rows(self):
        from robert_agent.runtime import cycle as run_once
        from robert_agent.adapters.sqlite import database as storage
        from robert_agent.work.application import commands as work_items

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        repo_id = "repo:example/backend"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, '{}')
                """,
                (
                    repo_id,
                    "example/backend",
                    "robert-bot",
                    "master",
                    str(self.repo_root),
                    str(self.worktree_root),
                ),
            )
        context = work_items.CommandContext(
            actor_kind="operator",
            actor_identity="wklken",
            allowed_repo_ids=frozenset({repo_id}),
            allowed_workers=frozenset({"default"}),
        )
        created = work_items.create_work_item(
            db_path,
            context=context,
            repo_id=repo_id,
            title="Analyze the task control boundary",
            description="Explain how local assignments enter the existing execution engine.",
            priority="P1",
            routing_mode="auto",
            requested_worker=None,
            start=True,
            idempotency_key="local-create-1",
        )
        empty_fixture = self.root / "empty.json"
        empty_fixture.write_text('{"events": []}', encoding="utf-8")

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=empty_fixture,
            dry_run=True,
            skip_external=True,
        )
        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=empty_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "work_items",
                    "workstreams",
                    "tasks",
                    "attempts",
                    "artifacts",
                    "github_sources",
                    "github_events",
                    "task_events",
                    "worker_results",
                )
            }
            task = conn.execute("SELECT task_id, route_id, expected_output, lifecycle FROM tasks").fetchone()
            attempt = conn.execute("SELECT attempt_id, status FROM attempts").fetchone()
            prompt_path = conn.execute("SELECT path FROM artifacts WHERE artifact_type = 'prompt'").fetchone()[0]
            wakeup_status = conn.execute(
                "SELECT status FROM wakeups WHERE work_item_id = ?",
                (created["item"]["work_item_id"],),
            ).fetchone()[0]
        self.assertEqual(counts["work_items"], 1)
        self.assertEqual(counts["workstreams"], 1)
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual(counts["attempts"], 1)
        self.assertEqual(counts["github_sources"], 0)
        self.assertEqual(counts["github_events"], 0)
        self.assertEqual(counts["task_events"], 0)
        self.assertEqual(counts["worker_results"], 0)
        self.assertEqual(task[1:3], ("local-result", "local_result"))
        self.assertEqual(attempt[1], "prepared")
        self.assertIn("local assignment", Path(prompt_path).read_text(encoding="utf-8"))
        self.assertEqual(wakeup_status, "consumed")

    def test_manual_task_worker_overrides_route_and_default_worker(self):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / "manual-worker.sqlite3"
        storage.init_database(db_path)
        prompt_path = self.root / "manual-prompt.md"
        prompt_path.write_text("manual task", encoding="utf-8")
        now = "2026-07-19T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'robot', 'main', ?, ?)",
                (str(self.repo_root), str(self.worktree_root)),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at) VALUES ('local:wi-1', 'repo-1', 'active', 'task-1', ?, ?)",
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, lifecycle, priority, routing_mode,
                  requested_worker, route_id, expected_output, created_at, updated_at
                ) VALUES ('task-1', 'local:wi-1', 'queued', 'P1', 'manual', 'reviewer',
                          'local-result', 'local_result', ?, ?)
                """,
                (now, now),
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, metadata_json) VALUES ('attempt-1', 'task-1', 1, 'prepared', ?, '{\"result_contract\":\"compact-v1\"}')",
                (now,),
            )
            conn.execute(
                "INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at) VALUES ('artifact-1', 'task-1', 'attempt-1', 'prompt', ?, ?)",
                (str(prompt_path), now),
            )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id=?",
                (json.dumps({"trigger_work_item_event_id": "local-request"}), "task-1"),
            )

        workers = [
            {
                "name": "default",
                "agent": "cbc",
                "command": "cbc",
                "default_model": "model-default",
                "default_effort": "medium",
            },
            {
                "name": "reviewer",
                "agent": "codex",
                "command": "codex",
                "default_model": "model-review",
                "default_effort": "high",
            },
        ]
        calls = []
        original = cycle_worker.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "planned", "command": []}

        try:
            cycle_worker.dispatch_worker = fake_dispatch_worker
            dispatch_queue.dispatch_queued(
                db_path,
                ["repo-1"],
                [
                    {
                        "task_info": {
                            "task_id": "task-1",
                            "attempt_id": "attempt-1",
                            "workstream_id": "local:wi-1",
                            "route_id": "local-result",
                            "prompt_path": prompt_path,
                            "routing_mode": "manual",
                            "requested_worker": "reviewer",
                        },
                        "worktree_result": None,
                        "repo_id": "repo-1",
                        "repo": {"max_concurrency": 1},
                    }
                ],
                True,
                1,
                workers=workers,
                default_worker=workers[0],
                config_result={
                    "default_worker": workers[0],
                    "workers": workers,
                    "routes": {
                        "local-result": {
                            "worker": "default",
                            "model": "route-model",
                            "effort": "low",
                        }
                    },
                },
            )
        finally:
            cycle_worker.dispatch_worker = original

        self.assertEqual(calls[0]["worker_command"], "codex")
        self.assertEqual(calls[0]["model"], "model-review")
        self.assertEqual(calls[0]["reasoning_effort"], "high")

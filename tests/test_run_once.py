from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tests.support import PACKAGE_ROOT


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


def _dd_comment_body(text, task_id="task-1", attempt_id="attempt-1", fingerprints="comment:1"):
    return (
        f"<!-- robert-comment task_id={task_id} attempt_id={attempt_id} "
        f"event_fingerprints={fingerprints} -->\n{text}"
    )


def _dd_pr_body(text, task_id="task-1", issue_number="123"):
    return (
        "<!-- robert-workstream\n"
        f"origin_workstream_id: github:example/backend#{issue_number}\n"
        f"source_issue: {issue_number}\n"
        f"task_id: {task_id}\n"
        "created_by: robert\n"
        "-->\n"
        + text
    )


def _open_pr_action(task_id="task-1", issue_number="123", pr_number="9"):
    return {
        "type": "open_pr",
        "repo": "example/backend",
        "head": f"codex/dd-{issue_number}-task",
        "base": "master",
        "title": f"Fix issue {issue_number}",
        "body": _dd_pr_body("Opened PR", task_id=task_id, issue_number=issue_number),
        "url": f"https://github.com/example/backend/pull/{pr_number}",
    }


def _new_pr_actions(task_id="task-1", issue_number="123", pr_number="9"):
    return [
        {
            "type": "push_existing_pr",
            "worktree_path": f"/tmp/.worktrees/codex__dd-{issue_number}-task",
            "branch": f"codex/dd-{issue_number}-task",
        },
        _open_pr_action(task_id=task_id, issue_number=issue_number, pr_number=pr_number),
    ]


def _review_point_evaluation(verdict="correct", action="implement"):
    return [
        {
            "summary": "The review point asks for a bounded follow-up fix.",
            "verdict": verdict,
            "reasoning": "The worker checked the current code path before choosing an action.",
            "action": action,
        }
    ]


def _verification_evidence(command="python -m unittest", status="passed", required=True):
    entry = {
        "command": command,
        "status": status,
        "purpose": "Verify the worker result before agent publication.",
        "required": required,
    }
    if status != "skipped":
        entry["exit_code"] = 0 if status == "passed" else 1
    else:
        entry["skipped_reason"] = "No command was required for this route."
    return entry


class _Completed:
    def __init__(self, args, stdout):
        self.args = args
        self.stdout = stdout


class RunOnceDryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "blueking-apigateway"
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
  - full_name: example/backend
    github_account: robert-bot
    trusted_actors:
      - wklken
    default_base_branch: master
    repo_root: {self.repo_root}
    worktree_root: {self.worktree_root}
""",
            encoding="utf-8",
        )
        self.fixture_path = self.root / "discovery.json"
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this bug",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/issues/123",
                            "state": "open",
                            "source_updated_at": "2026-06-16T00:00:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def _seed_active_pr_task(self, pr_number):
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-06-22T12:00:00+00:00"
        task_id = f"task-pr-{pr_number}"
        attempt_id = f"attempt-pr-{pr_number}"
        source_key = f"github:example/backend!{pr_number}"
        source_id = f"source:{source_key}"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
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
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, html_url, title, state, author_login)
                VALUES (?, 'repo:example/backend', ?, 'pull_request', ?, ?, 'Fix compatibility bug', 'open', 'robert-bot')
                """,
                (
                    source_id,
                    source_key,
                    pr_number,
                    f"https://github.com/example/backend/pull/{pr_number}",
                ),
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES (?, 'repo:example/backend', ?, 'github:example/backend#123', 'active', ?, ?, ?)
                """,
                (source_key, source_id, task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES (?, ?, 'queued', 'P1', 'update-existing-pr', 'update_existing_pr', ?, ?)
                """,
                (task_id, source_key, now, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES (?, ?, 1, 'prepared', ?, ?)
                """,
                (attempt_id, task_id, now, now),
            )
        return db_path, task_id, attempt_id

    def test_dry_run_fixture_creates_control_plane_rows_and_prompt_artifact(self):
        from robert_agent import run_once
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
            route = conn.execute(
                "SELECT expected_output, recommended_skills_json FROM route_decisions"
            ).fetchone()

            source = conn.execute(
                "SELECT html_url, state, source_updated_at FROM github_sources"
            ).fetchone()
            attempt = conn.execute(
                "SELECT status, metadata_json, worktree_path, branch_name FROM attempts"
            ).fetchone()
            artifacts = dict(
                conn.execute("SELECT artifact_type, path FROM artifacts").fetchall()
            )
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
        self.assertIn("robert-workstream", prompt)
        self.assertIn("allowed_github_actions", prompt)
        self.assertIn("worktree_path:", prompt)
        self.assertIn("target_base_branch: master", prompt)
        self.assertIn(str(PACKAGE_ROOT / "worker_result.py"), prompt)
        self.assertIn(str(db_path), prompt)
        self.assertIn("github-context.json", prompt)
        self.assertIn("github-context.md", prompt)
        self.assertNotIn("@robert-bot please fix this bug", prompt)
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
        from robert_agent import run_once
        from robert_agent import storage, work_items

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
                    "work_items", "workstreams", "tasks", "attempts", "artifacts",
                    "github_sources", "github_events", "task_events", "worker_results",
                )
            }
            task = conn.execute(
                "SELECT task_id, route_id, expected_output, lifecycle FROM tasks"
            ).fetchone()
            attempt = conn.execute(
                "SELECT attempt_id, status FROM attempts"
            ).fetchone()
            prompt_path = conn.execute(
                "SELECT path FROM artifacts WHERE artifact_type = 'prompt'"
            ).fetchone()[0]
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
        from robert_agent import run_once
        from robert_agent import storage

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
                "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at) VALUES ('attempt-1', 'task-1', 1, 'prepared', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at) VALUES ('artifact-1', 'task-1', 'attempt-1', 'prompt', ?, ?)",
                (str(prompt_path), now),
            )
        workers = [
            {
                "name": "default", "agent": "cbc", "command": "cbc",
                "default_model": "model-default", "default_effort": "medium",
            },
            {
                "name": "reviewer", "agent": "codex", "command": "codex",
                "default_model": "model-review", "default_effort": "high",
            },
        ]
        calls = []
        original = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "planned", "command": []}

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            run_once._dispatch_queued(
                db_path,
                ["repo-1"],
                [
                    {
                        "task_info": {
                            "task_id": "task-1", "attempt_id": "attempt-1",
                            "workstream_id": "local:wi-1", "route_id": "local-result",
                            "prompt_path": prompt_path, "routing_mode": "manual",
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
                route_worker_models={"local-result": {"worker": "default", "model": "route-model", "effort": "low"}},
            )
        finally:
            run_once.dispatch.dispatch_worker = original

        self.assertEqual(calls[0]["worker_command"], "codex")
        self.assertEqual(calls[0]["model"], "model-review")
        self.assertEqual(calls[0]["reasoning_effort"], "high")

    def test_repo_run_steps_can_record_same_step_for_different_repos(self):
        from robert_agent import run_once
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT INTO agent_runs(run_id, status, started_at, config_path, dry_run) VALUES (?, 'running', ?, ?, 1)",
                ("run-test", "2026-07-04T00:00:00+00:00", str(self.config_path)),
            )
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, ?, ?, ?, ?)",
                ("repo:a", "Org/a", "robot", "main", str(self.repo_root), str(self.worktree_root)),
            )
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, ?, ?, ?, ?)",
                ("repo:b", "Org/b", "robot", "main", str(self.repo_root), str(self.worktree_root)),
            )

            run_once._insert_repo_run_step(conn, "run-test", "repo:a", "discover")
            run_once._insert_repo_run_step(conn, "run-test", "repo:b", "discover")
            run_once._mark_repo_run_step(
                conn,
                "run-test",
                "repo:a",
                "discover",
                "succeeded",
                "2026-07-04T00:00:01+00:00",
                output={"raw_event_count": 1},
            )

            rows = conn.execute(
                "SELECT repo_id, step_key, status, output_json FROM run_repo_steps ORDER BY repo_id"
            ).fetchall()

        self.assertEqual([(row[0], row[1], row[2]) for row in rows], [
            ("repo:a", "discover", "succeeded"),
            ("repo:b", "discover", "pending"),
        ])
        self.assertIn("raw_event_count", rows[0][3])

    def test_run_once_processes_two_repos_with_repo_specific_trust(self):
        from robert_agent import run_once
        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        config_path = self.config_path
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_base_branch: main
    repo_root: {repo_b}
    worktree_root: {worktree_b}
""",
            encoding="utf-8",
        )

        def fake_collect(repo, known_workstreams=None, notification_hints=None, **_kwargs):
            if repo["full_name"] == "example/backend":
                return [
                    {
                        "id": "comment-a",
                        "number": 1,
                        "source_type": "issue",
                        "event_type": "comment",
                        "actor_login": "wklken",
                        "author_association": "MEMBER",
                        "body": f"@{repo['github_account']} please analyze",
                    }
                ]
            if repo["full_name"] == "Example/other":
                return [
                    {
                        "id": "comment-b",
                        "number": 2,
                        "source_type": "issue",
                        "event_type": "comment",
                        "actor_login": "other-maintainer",
                        "author_association": "MEMBER",
                        "body": f"@{repo['github_account']} please analyze",
                    }
                ]
            raise AssertionError(repo)

        original_collect = run_once.discover.collect_live_events
        try:
            run_once.discover.collect_live_events = fake_collect
            result = run_once.run_once(
                config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            run_once.discover.collect_live_events = original_collect

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        with closing(sqlite3.connect(result["db_path"])) as conn:
            repos = conn.execute("SELECT full_name FROM repos ORDER BY full_name").fetchall()
            tasks = conn.execute(
                """
                SELECT r.full_name, COUNT(*)
                FROM tasks t
                JOIN workstreams w ON w.workstream_id = t.workstream_id
                JOIN repos r ON r.repo_id = w.repo_id
                GROUP BY r.full_name
                ORDER BY r.full_name
                """
            ).fetchall()
            repo_steps = conn.execute(
                "SELECT COUNT(*) FROM run_repo_steps WHERE step_key = 'route'"
            ).fetchone()[0]
            run_summary = json.loads(
                conn.execute(
                    "SELECT summary_json FROM agent_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()[0]
            )

        self.assertEqual(
            [row[0] for row in repos],
            ["Example/other", "example/backend"],
        )
        self.assertEqual(tasks, [("Example/other", 1), ("example/backend", 1)])
        self.assertEqual(repo_steps, 2)
        self.assertEqual(len(run_summary["repo_summaries"]), 2)

    def test_run_once_continues_after_one_repo_discovery_failure(self):
        from robert_agent import run_once
        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_base_branch: main
    repo_root: {repo_b}
    worktree_root: {worktree_b}
""",
            encoding="utf-8",
        )

        def fake_collect(repo, **_kwargs):
            if repo["full_name"] == "example/backend":
                raise subprocess.CalledProcessError(1, ["gh", "search", "issues"])
            return [
                {
                    "id": "comment-b",
                    "number": 2,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": "other-maintainer",
                    "author_association": "MEMBER",
                    "body": f"@{repo['github_account']} please analyze",
                }
            ]

        original_collect = run_once.discover.collect_live_events
        try:
            run_once.discover.collect_live_events = fake_collect
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            run_once.discover.collect_live_events = original_collect

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(len(result["repo_summaries"]), 2)
        with closing(sqlite3.connect(result["db_path"])) as conn:
            run = conn.execute(
                "SELECT status, summary_json FROM agent_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            active_leases = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE resource_type = 'agent_run' AND status = 'active'"
            ).fetchone()[0]

        self.assertEqual(run[0], "failed")
        self.assertEqual(json.loads(run[1])["overall_status"], "partial_failure")
        self.assertEqual(active_leases, 0)

    def test_run_once_multi_repo_top_level_notifications_disable_repo_notification_polling(self):
        from robert_agent import run_once
        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_base_branch: main
    repo_root: {repo_b}
    worktree_root: {worktree_b}
""",
            encoding="utf-8",
        )

        discovery_calls = []

        def fake_collect_account_notifications(repos, runner=None):
            self.assertEqual(
                [repo["full_name"] for repo in repos],
                ["example/backend", "Example/other"],
            )
            self.assertIsNotNone(runner)
            return {
                "example/backend": [
                    {
                        "id": "notification-a",
                        "reason": "mention",
                        "repository": {
                            "full_name": "example/backend"
                        },
                    }
                ]
            }

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "known_workstreams": known_workstreams,
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            actor_login = "wklken"
            if repo["full_name"] == "Example/other":
                actor_login = "other-maintainer"
            return [
                {
                    "id": f"comment-{repo['full_name']}",
                    "number": 1 if repo["full_name"] == "example/backend" else 2,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": actor_login,
                    "author_association": "MEMBER",
                    "body": f"@{repo['github_account']} please analyze",
                }
            ]

        original_collect_notifications = run_once.discover.collect_account_notifications
        original_collect_live_events = run_once.discover.collect_live_events
        try:
            run_once.discover.collect_account_notifications = fake_collect_account_notifications
            run_once.discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            run_once.discover.collect_account_notifications = original_collect_notifications
            run_once.discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "known_workstreams": set(),
                    "notification_hints": [
                        {
                            "id": "notification-a",
                            "reason": "mention",
                            "repository": {
                                "full_name": "example/backend"
                            },
                        }
                    ],
                    "include_notifications": False,
                },
                {
                    "repo": "Example/other",
                    "known_workstreams": set(),
                    "notification_hints": [],
                    "include_notifications": False,
                },
            ],
        )

    def test_run_once_single_repo_empty_top_level_notifications_disable_repo_polling(self):
        from robert_agent import run_once
        discovery_calls = []

        def fake_collect_account_notifications(repos, runner=None):
            self.assertEqual(
                [repo["full_name"] for repo in repos],
                ["example/backend"],
            )
            self.assertIsNotNone(runner)
            return {}

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            return []

        original_collect_notifications = run_once.discover.collect_account_notifications
        original_collect_live_events = run_once.discover.collect_live_events
        try:
            run_once.discover.collect_account_notifications = fake_collect_account_notifications
            run_once.discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            run_once.discover.collect_account_notifications = original_collect_notifications
            run_once.discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "notification_hints": [],
                    "include_notifications": False,
                },
            ],
        )

    def test_run_once_multi_repo_notification_collection_failure_falls_back_per_repo(self):
        from robert_agent import run_once
        repo_b = self.root / "repo-b"
        repo_b.mkdir()
        (repo_b / ".git").mkdir()
        worktree_b = repo_b / ".worktrees"
        worktree_b.mkdir()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + f"""  - full_name: Example/other
    github_account: robot-other
    trusted_actors:
      - other-maintainer
    default_base_branch: main
    repo_root: {repo_b}
    worktree_root: {worktree_b}
""",
            encoding="utf-8",
        )

        discovery_calls = []

        def fake_collect_account_notifications(_repos, runner=None):
            raise subprocess.CalledProcessError(1, ["gh", "api", "notifications"])

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            actor_login = "wklken"
            if repo["full_name"] == "Example/other":
                actor_login = "other-maintainer"
            return [
                {
                    "id": f"comment-{repo['full_name']}",
                    "number": 1 if repo["full_name"] == "example/backend" else 2,
                    "source_type": "issue",
                    "event_type": "comment",
                    "actor_login": actor_login,
                    "author_association": "MEMBER",
                    "body": f"@{repo['github_account']} please analyze",
                }
            ]

        original_collect_notifications = run_once.discover.collect_account_notifications
        original_collect_live_events = run_once.discover.collect_live_events
        try:
            run_once.discover.collect_account_notifications = fake_collect_account_notifications
            run_once.discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            run_once.discover.collect_account_notifications = original_collect_notifications
            run_once.discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "notification_hints": None,
                    "include_notifications": True,
                },
                {
                    "repo": "Example/other",
                    "notification_hints": None,
                    "include_notifications": True,
                },
            ],
        )

    def test_run_once_notification_command_missing_falls_back_per_repo(self):
        from robert_agent import run_once
        discovery_calls = []

        def fake_collect_account_notifications(_repos, runner=None):
            raise FileNotFoundError("gh")

        def fake_collect_live_events(
            repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            discovery_calls.append(
                {
                    "repo": repo["full_name"],
                    "notification_hints": notification_hints,
                    "include_notifications": include_notifications,
                }
            )
            return []

        original_collect_notifications = run_once.discover.collect_account_notifications
        original_collect_live_events = run_once.discover.collect_live_events
        try:
            run_once.discover.collect_account_notifications = fake_collect_account_notifications
            run_once.discover.collect_live_events = fake_collect_live_events
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=False,
            )
        finally:
            run_once.discover.collect_account_notifications = original_collect_notifications
            run_once.discover.collect_live_events = original_collect_live_events

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            discovery_calls,
            [
                {
                    "repo": "example/backend",
                    "notification_hints": None,
                    "include_notifications": True,
                },
            ],
        )

    def test_dry_run_respects_max_dispatches(self):
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this bug",
                            "intent": "bug_fix",
                        },
                        {
                            "id": "comment-2",
                            "number": 124,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please add tests",
                            "intent": "add_tests",
                        },
                        {
                            "id": "comment-3",
                            "number": 125,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please make a small change",
                            "intent": "small_change",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
            max_dispatches=1,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["dispatch_count"], 1)
        self.assertEqual(len(result["prompt_paths"]), 3)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            dispatched_attempt_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM attempts
                WHERE json_extract(metadata_json, '$.dispatch.status') = 'prepared'
                """
            ).fetchone()[0]
        self.assertEqual(task_count, 3)
        self.assertEqual(dispatched_attempt_count, 1)

    def test_second_run_preserves_existing_control_plane_rows(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            first_task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            first_attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_rows = conn.execute("SELECT task_id FROM tasks").fetchall()
            attempt_rows = conn.execute("SELECT attempt_id FROM attempts").fetchall()
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            event_status = conn.execute(
                """
                SELECT authorization_status
                FROM github_events
                WHERE event_fingerprint = 'comment:comment-1'
                """
            ).fetchone()[0]
        self.assertIn((first_task_id,), task_rows)
        self.assertIn((first_attempt_id,), attempt_rows)
        self.assertEqual(len(task_rows), 1)
        self.assertEqual(len(attempt_rows), 1)
        self.assertEqual(result_count, 0)
        self.assertEqual(event_status, "authorized_trigger")

    def test_existing_workstream_followup_is_context_not_new_task(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "@robert-bot this extra detail may matter",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            relationships = {
                row[0]
                for row in conn.execute("SELECT relationship FROM task_events ORDER BY relationship")
            }
        self.assertEqual(task_count, 1)
        self.assertEqual(relationships, {"context", "trigger"})

    def test_existing_workstream_followup_without_mention_is_ignored(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "this extra detail may matter",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            relationships = {
                row[0]
                for row in conn.execute("SELECT relationship FROM task_events ORDER BY relationship")
            }
            event_status = conn.execute(
                """
                SELECT authorization_status
                FROM github_events
                WHERE event_fingerprint = 'comment:comment-2'
                """
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(relationships, {"trigger"})
        self.assertEqual(event_status, "ignored_context")

    def test_active_issue_workstream_does_not_capture_pr_followup_task(self):
        from robert_agent import run_once
        issue_fixture = self.root / "issue.json"
        issue_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "issue-comment-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this",
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pr_fixture = self.root / "pr.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-comment-1",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot follow up",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-123-fix",
                            "metadata": {
                                "dd_workstream": {
                                    "origin_workstream_id": "github:example/backend#123",
                                    "source_issue": "123",
                                }
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=issue_fixture,
            dry_run=True,
            skip_external=True,
        )
        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            workstream_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT workstream_id FROM tasks ORDER BY created_at"
                )
            ]
        self.assertEqual(
            workstream_ids,
            [
                "github:example/backend#123",
                "github:example/backend!456",
            ],
        )

    def test_member_context_on_known_inactive_pr_workstream_does_not_create_task(self):
        from robert_agent import run_once
        db_path = self.data_dir / "dd.sqlite3"
        initial = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(initial["ok"], initial)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, state, author_login
                )
                VALUES (?, ?, ?, 'pull_request', 456, '', 'open', 'robert-bot')
                """,
                (
                    "source:github:example/backend!456",
                    "repo:example/backend",
                    "github:example/backend!456",
                ),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, ?, ?, 'completed', NULL, ?, ?)
                """,
                (
                    "github:example/backend!456",
                    "repo:example/backend",
                    "source:github:example/backend!456",
                    now,
                    now,
                ),
            )

        pr_fixture = self.root / "inactive-known-pr.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-member-followup",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "@robert-bot follow-up detail, please fix this bug",
                            "workstream_id": "github:example/backend!456",
                            "has_open_dd_pr": True,
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            pr_task_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE workstream_id = 'github:example/backend!456'
                """
            ).fetchone()[0]
            event_row = conn.execute(
                """
                SELECT authorization_status, actor_login, event_fingerprint
                FROM github_events
                WHERE event_fingerprint = 'comment:pr-member-followup'
                """
            ).fetchone()
        self.assertEqual(pr_task_count, 0)
        self.assertEqual(event_row, ("accepted_context", "reviewer", "comment:pr-member-followup"))

    def test_trusted_context_on_known_inactive_pr_workstream_creates_new_task(self):
        from robert_agent import run_once
        db_path = self.data_dir / "dd.sqlite3"
        initial = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(initial["ok"], initial)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number, title, state, author_login
                )
                VALUES (?, ?, ?, 'pull_request', 456, '', 'open', 'robert-bot')
                """,
                (
                    "source:github:example/backend!456",
                    "repo:example/backend",
                    "github:example/backend!456",
                ),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at
                )
                VALUES (?, ?, ?, 'completed', NULL, ?, ?)
                """,
                (
                    "github:example/backend!456",
                    "repo:example/backend",
                    "source:github:example/backend!456",
                    now,
                    now,
                ),
            )

        pr_fixture = self.root / "inactive-known-pr-trusted.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-trusted-followup",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "OWNER",
                            "body": "@robert-bot please analyze this follow-up",
                            "workstream_id": "github:example/backend!456",
                            "has_open_dd_pr": True,
                            "intent": "analysis",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            rows = conn.execute(
                """
                SELECT workstream_id, lifecycle
                FROM tasks
                WHERE workstream_id = 'github:example/backend!456'
                """
            ).fetchall()
        self.assertEqual(rows, [("github:example/backend!456", "queued")])

    def test_audited_worker_result_with_unpublished_action_keeps_task_active(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
            wakeup_status = conn.execute(
                "SELECT status FROM wakeups WHERE result_id = ?",
                (record["result_id"],),
            ).fetchone()[0]
            relationship = conn.execute(
                "SELECT relationship FROM task_events"
            ).fetchone()[0]
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(action_status, ("accepted", "not_published"))
        self.assertEqual(wakeup_status, "consumed")
        self.assertEqual(relationship, "trigger")

    def test_run_once_rejects_publishable_result_without_required_verification(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [],
                "handoff": "opened PR without verification",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
            audit_metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )["audit"]
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(action_status, ("failed", "not_published"))
        self.assertEqual(audit_metadata["status"], "failed")
        self.assertIn("verification policy failed", audit_metadata["safe_error"])

    def test_run_once_copies_worker_usage_from_stdout_log(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        stdout_path = self.root / "worker.stdout.log"
        stdout_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "message", "usage": {"input_tokens": 1}}),
                    json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "duration_ms": 1234,
                            "num_turns": 1,
                            "total_cost_usd": 0.045,
                            "usage": {
                                "input_tokens": 20,
                                "output_tokens": 8,
                            },
                        },
                        sort_keys=True,
                    ),
                ]
            ),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json = ? WHERE attempt_id = ?",
                (
                    json.dumps(
                        {"dispatch": {"stdout_path": str(stdout_path)}},
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            )
            result_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id = ?",
                    (record["result_id"],),
                ).fetchone()[0]
            )
        self.assertTrue(attempt_metadata["usage"]["usage_available"])
        self.assertTrue(result_metadata["usage"]["usage_available"])
        self.assertEqual(result_metadata["usage"]["usage"]["input_tokens"], 20)
        self.assertEqual(result_metadata["usage"]["total_cost_usd"], 0.045)

    def test_run_once_records_publish_actions_step_after_audit(self):
        from robert_agent import run_once
        calls = []
        original_publish = run_once.publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            calls.append((str(db_path), dry_run))
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            run_once.publish.publish_ready_actions = fake_publish_ready_actions
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls, [(str(self.data_dir / "dd.sqlite3"), False)])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            step = conn.execute(
                """
                SELECT status, output_json
                FROM run_steps
                WHERE step_key = 'publish_actions'
                """
            ).fetchone()
        self.assertEqual(step[0], "succeeded")
        self.assertEqual(json.loads(step[1])["published_count"], 1)

    def test_skip_publish_skips_publish_step_and_does_not_invoke_publish(self):
        from robert_agent import run_once
        original_publish = run_once.publish.publish_ready_actions
        original_finalize = run_once._finalize_published_results

        def unexpected_publish(*_args, **_kwargs):
            raise AssertionError("publish should not run when skip_publish=True")

        def unexpected_finalize(*_args, **_kwargs):
            raise AssertionError("finalize should not run when skip_publish=True")

        try:
            run_once.publish.publish_ready_actions = unexpected_publish
            run_once._finalize_published_results = unexpected_finalize
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
                skip_publish=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish
            run_once._finalize_published_results = original_finalize

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            step = conn.execute(
                """
                SELECT status, output_json
                FROM run_steps
                WHERE step_key = 'publish_actions'
                """
            ).fetchone()
        self.assertEqual(step[0], "skipped")
        self.assertEqual(json.loads(step[1])["reason"], "skip_publish")

    def test_publish_step_finalizes_task_after_action_is_published(self):
        from robert_agent.worker import result
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "planned_github_actions": [
                    {
                        "type": "comment",
                        "target_url": "https://github.com/example/backend/issues/123",
                        "body": _dd_comment_body(
                            "Analysis is ready",
                            task_id=task_id,
                            attempt_id=attempt_id,
                            fingerprints="comment:comment-analysis",
                        ),
                    }
                ],
                "consumed_event_fingerprints": ["comment:comment-analysis"],
                "verification": [_verification_evidence()],
                "handoff": "ready",
                "used_skills": ["fast-code-path"],
            },
        )
        self.assertTrue(record["ok"], record)

        original_publish = run_once.publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE github_actions
                    SET publish_status = 'published',
                        external_id = '987',
                        target_url = 'https://github.com/example/backend/issues/123#issuecomment-987'
                    WHERE result_id = ?
                    """,
                    (record["result_id"],),
                )
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            run_once.publish.publish_ready_actions = fake_publish_ready_actions
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            relationship = conn.execute(
                "SELECT relationship FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            publish_step = json.loads(
                conn.execute(
                    "SELECT output_json FROM run_steps WHERE step_key = 'publish_actions' ORDER BY started_at DESC LIMIT 1"
                ).fetchone()[0]
            )
        self.assertEqual(task_lifecycle, "completed")
        self.assertEqual(workstream, ("completed", None))
        self.assertEqual(relationship, "consumed")
        self.assertEqual(publish_step["finalized_task_count"], 1)

    def test_completed_consumed_trigger_is_not_reprocessed(self):
        from robert_agent.worker import result
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "unclear-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please look at this",
                            "intent": "unclear",
                        }
                    ]
                }
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "planned_github_actions": [],
                "consumed_event_fingerprints": ["comment:unclear-1"],
                "verification": [],
                "handoff": "No action needed",
                "used_skills": [],
            },
        )
        self.assertTrue(record["ok"], record)
        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(finalized["ok"], finalized)

        replay = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(replay["ok"], replay)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            relationship = conn.execute(
                "SELECT relationship FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(task_count, 1)
        self.assertEqual(relationship, "consumed")

    def test_classification_result_legacy_recommend_route_handoff_creates_child_task(self):
        from robert_agent.worker import result
        from robert_agent import run_once
        from robert_agent import board

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "body": "@robert-bot please classify this",
                            "intent": "unclear",
                            "title": "Bug needs implementation",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
                        }
                    ]
                }
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "planned_github_actions": [],
                "consumed_event_fingerprints": ["assigned:assign-2904"],
                "verification": [],
                "handoff": "Recommend route new-pr",
                "used_skills": [],
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        self.assertEqual(finalized["dispatch_count"], 1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute(
                """
                SELECT task_id, parent_task_id, lifecycle, route_id, expected_output
                FROM tasks
                ORDER BY created_at, task_id
                """
            ).fetchall()
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            child_relationship = conn.execute(
                """
                SELECT te.relationship
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                  AND ge.event_fingerprint = 'assigned:assign-2904'
                """,
                (tasks[1][0],),
            ).fetchone()[0]
            work_item_id = conn.execute(
                "SELECT work_item_id FROM work_items"
            ).fetchone()[0]
        detail = board.get_work_item_detail(db_path, work_item_id)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0][2], "completed")
        self.assertEqual(tasks[1][1], tasks[0][0])
        self.assertEqual(tasks[1][2:], ("queued", "new-pr", "new_pr"))
        self.assertEqual(workstream, ("active", tasks[1][0]))
        self.assertEqual(child_relationship, "trigger")
        self.assertEqual(detail["column"], "todo")
        self.assertEqual(detail["reason_code"], "queued")
        self.assertEqual(detail["attention_signals"], [])
        self.assertIsNotNone(detail["activated_at"])

    def test_classification_result_child_keeps_canonical_trigger_payload(self):
        from robert_agent.worker import result
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "assignment_actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-2904",
                            "body": "背景：dashboard admin missing fields\n需求：add readonly fields",
                            "intent": "unclear",
                            "title": "Bug needs implementation",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
                        },
                        {
                            "id": "notification-2904",
                            "number": 2904,
                            "source_type": "issue",
                            "event_type": "notification",
                            "actor_login": "",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-2904",
                            "body": "",
                            "intent": "unclear",
                            "title": "Bug needs implementation",
                            "url": "https://github.com/example/backend/issues/2904",
                            "state": "open",
                        },
                    ]
                }
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "planned_github_actions": [],
                "consumed_event_fingerprints": ["assigned:assign-2904"],
                "verification": [],
                "handoff": "Recommend route new-pr",
                "used_skills": [],
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            child_task_id = conn.execute(
                """
                SELECT task_id
                FROM tasks
                WHERE parent_task_id = ?
                """,
                (task_id,),
            ).fetchone()[0]
            context_path = conn.execute(
                """
                SELECT path
                FROM artifacts
                WHERE task_id = ?
                  AND artifact_type = 'github_context_json'
                """,
                (child_task_id,),
            ).fetchone()[0]
        child_context = json.loads(Path(context_path).read_text(encoding="utf-8"))
        child_event = child_context["events"][0]
        self.assertEqual(child_event["event_type"], "assigned")
        self.assertEqual(child_event["authorization_status"], "authorized_trigger")
        self.assertEqual(
            child_event["body"],
            "背景：dashboard admin missing fields\n需求：add readonly fields",
        )

    def test_classification_result_branch_slug_names_child_worktree(self):
        from robert_agent import run_once
        from robert_agent.worker import result

        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "assign-3022",
                            "number": 3022,
                            "source_type": "issue",
                            "event_type": "assigned",
                            "actor_login": "wklken",
                            "assignment_actor_login": "wklken",
                            "assigned_to": "robert-bot",
                            "authorization_lookup_complete": True,
                            "event_fingerprint": "assigned:assign-3022",
                            "body": "实现模型服务连通性测试并拉取支持的模型列表",
                            "intent": "unclear",
                            "title": "模型服务连通性测试",
                            "url": "https://github.com/example/backend/issues/3022",
                            "state": "open",
                        }
                    ]
                }
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
        with closing(sqlite3.connect(db_path)) as conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "classification_result",
                "planned_github_actions": [],
                "consumed_event_fingerprints": ["assigned:assign-3022"],
                "verification": [],
                "handoff": "Implement the requested connectivity test",
                "used_skills": [],
                "recommended_route": "new-pr",
                "branch_slug": "model-service-connectivity-test",
            },
        )
        self.assertTrue(record["ok"], record)

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(finalized["ok"], finalized)
        with closing(sqlite3.connect(db_path)) as conn:
            branch_name = conn.execute(
                """
                SELECT a.branch_name
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE t.parent_task_id = ?
                """,
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(branch_name, "codex/dd-3022-model-service-connectivity-test")

    def test_retry_after_failed_task_includes_original_trigger_as_context(self):
        from robert_agent import run_once
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
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed' WHERE task_id = ?", (task_id,)
            )
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE attempt_id = ?",
                ("2026-06-23T04:00:00+00:00", attempt_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL
                """
            )

        followup_fixture = self.root / "followup.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please also add tests",
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute(
                "SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at"
            ).fetchall()
            child_events = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.relationship, ge.event_at
                """,
                (tasks[1][0],),
            ).fetchall()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1][1], task_id)
        fingerprints = {row[1] for row in child_events}
        self.assertIn("comment:comment-1", fingerprints)
        self.assertIn("comment:comment-2", fingerprints)
        trigger_row = [row for row in child_events if row[0] == "trigger"]
        self.assertEqual(len(trigger_row), 1)

    def test_retry_after_failed_task_keeps_original_trigger_primary(self):
        from robert_agent import run_once
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
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed' WHERE task_id = ?", (task_id,)
            )
            conn.execute(
                "UPDATE attempts SET status = 'failed', finished_at = ? WHERE attempt_id = ?",
                ("2026-06-23T04:00:00+00:00", attempt_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL
                """
            )

        followup_fixture = self.root / "followup-mentioned.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-2",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please also add tests",
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute(
                "SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at"
            ).fetchall()
            child_events = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY CASE te.relationship WHEN 'trigger' THEN 0 ELSE 1 END, ge.event_at
                """,
                (tasks[1][0],),
            ).fetchall()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1][1], task_id)
        self.assertEqual(child_events[0], ("trigger", "comment:comment-1"))
        self.assertIn(("context", "comment:comment-2"), child_events)

    def test_publish_failure_does_not_skip_unrelated_routing(self):
        from robert_agent import run_once
        original_publish = run_once.publish.publish_ready_actions

        def fake_publish_ready_actions(_db_path, dry_run=False, repo_id=None):
            return {
                "ok": False,
                "status": "publish_failed",
                "pending_count": 1,
                "published_count": 0,
                "deduplicated_count": 0,
                "skipped_count": 0,
                "failed_count": 1,
                "failures": [{"action_id": "action-old", "safe_error": "old PR already exists"}],
            }

        try:
            run_once.publish.publish_ready_actions = fake_publish_ready_actions
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "publish_failed")
        self.assertEqual(len(result["prompt_paths"]), 1)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            step_statuses = dict(
                conn.execute("SELECT step_key, status FROM run_steps")
            )
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(step_statuses["publish_actions"], "failed")
        self.assertEqual(step_statuses["discover"], "succeeded")
        self.assertEqual(step_statuses["authorize"], "succeeded")
        self.assertEqual(step_statuses["route"], "succeeded")
        self.assertEqual(step_statuses["dispatch"], "succeeded")
        self.assertEqual(step_statuses["summarize"], "succeeded")
        self.assertEqual(task_count, 1)

    def test_worker_result_missing_recommended_skill_is_accepted(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR",
                "used_skills": [],
            },
        )
        self.assertTrue(record["ok"], record)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
            audit_metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )["audit"]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(action_status, ("accepted", "not_published"))
        self.assertEqual(audit_metadata["status"], "accepted")
        self.assertIsNone(notification)

    def test_update_existing_pr_audit_uses_required_skill_and_review_evaluation(self):
        from robert_agent.worker import result
        from robert_agent import run_once
        pr_fixture = self.root / "pr-followup.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-followup",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this review point",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/dd-123-task",
                            "pr_author_login": "robert-bot",
                            "url": "https://github.com/example/backend/pull/456#discussion_r1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            required_skills, recommended_skills = conn.execute(
                "SELECT required_skills_json, recommended_skills_json FROM route_decisions"
            ).fetchone()
        self.assertEqual(json.loads(required_skills), [])
        self.assertIn("fast-verify-review-point", json.loads(recommended_skills))

        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "update_existing_pr",
                "planned_github_actions": [
                    {
                        "type": "push_existing_pr",
                        "worktree_path": str(self.worktree_root / "codex__dd-123-task"),
                        "branch": "codex/dd-123-task",
                    },
                    {
                        "type": "comment",
                        "target_url": "https://github.com/example/backend/pull/456#discussion_r1",
                        "body": _dd_comment_body("Implemented the valid review point and pushed the branch."),
                    }
                ],
                "consumed_event_fingerprints": ["comment:pr-followup"],
                "verification": [_verification_evidence()],
                "handoff": "updated existing PR",
                "used_skills": ["fast-verify-review-point"],
                "review_point_evaluation": _review_point_evaluation(),
            },
        )
        self.assertTrue(record["ok"], record)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            action_statuses = conn.execute(
                "SELECT action_type, audit_status, publish_status FROM github_actions ORDER BY action_type"
            ).fetchall()
            audit_metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )["audit"]
        self.assertEqual(
            action_statuses,
            [
                ("comment", "accepted", "not_published"),
                ("push_existing_pr", "accepted", "not_published"),
            ],
        )
        self.assertEqual(audit_metadata["status"], "accepted")

    def test_worker_result_output_type_must_match_route_expected_output(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "comment_analysis",
                "planned_github_actions": [
                    {
                        "type": "comment",
                        "target_url": "https://github.com/example/backend/issues/123",
                        "body": _dd_comment_body(
                            "Analysis only",
                            task_id=task_id,
                            attempt_id=attempt_id,
                            fingerprints="comment:comment-1",
                        ),
                    }
                ],
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [],
                "handoff": "analysis only",
                "used_skills": [],
            },
        )
        self.assertTrue(record["ok"], record)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
            audit_metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )["audit"]
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(action_status, ("failed", "not_published"))
        self.assertEqual(audit_metadata["status"], "failed")
        self.assertIn("expected_output new_pr", audit_metadata["safe_error"])

    def test_worker_result_is_not_reaudited_after_audit_metadata_recorded(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)
        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                UPDATE route_decisions
                SET allowed_github_actions_json = '[]'
                WHERE task_id = ?
                """,
                (task_id,),
            )

        reaudit = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(reaudit["ok"], reaudit)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions"
            ).fetchone()
            audit_metadata = json.loads(
                conn.execute("SELECT metadata_json FROM worker_results").fetchone()[0]
            )["audit"]
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(action_status, ("accepted", "not_published"))
        self.assertEqual(audit_metadata["status"], "accepted")

    def test_open_pr_result_materializes_pr_workstream_linked_to_origin_issue(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            pr_workstream = conn.execute(
                """
                SELECT workstream_id, origin_workstream_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!9'
                """
            ).fetchone()
        self.assertEqual(
            pr_workstream,
            (
                "github:example/backend!9",
                "github:example/backend#123",
            ),
        )

    def test_issue_to_pr_review_followup_keeps_workstreams_separate(self):
        from robert_agent.worker import result
        from robert_agent import run_once
        def fake_issue_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"] and "--assignee" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "search", "issues"] and "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-issue-123",
                          "updated_at": "2026-06-22T12:00:00Z",
                          "reason": "mention",
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/123"
                          },
                          "repository": {
                            "full_name": "example/backend"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(
                    args,
                    """{
                      "number": 123,
                      "state": "open",
                      "title": "Compatibility bug",
                      "updated_at": "2026-06-22T12:00:00Z",
                      "html_url": "https://github.com/example/backend/issues/123",
                      "user": {"login": "wklken"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-1",
                        "body": "@robert-bot please fix this bug",
                        "created_at": "2026-06-22T12:00:01Z",
                        "author_association": "OWNER",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        issue_run = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_issue_runner,
        )
        self.assertTrue(issue_run["ok"], issue_run)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            issue_task_id, issue_attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

        record = result.record_result(
            db_path,
            {
                "task_id": issue_task_id,
                "attempt_id": issue_attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(
                    task_id=issue_task_id,
                    issue_number="123",
                    pr_number="456",
                ),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR 456 for issue 123",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )

        finalized = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(finalized["ok"], finalized)

        def fake_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"] and "--assignee" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "search", "issues"] and "--mentions" in args:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-pr-456",
                          "updated_at": "2026-06-22T13:00:00Z",
                          "reason": "comment",
                          "subject": {
                            "type": "PullRequest",
                            "url": "https://api.github.com/repos/example/backend/pulls/456"
                          },
                          "repository": {
                            "full_name": "example/backend"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456"]:
                return _Completed(
                    args,
                    """{
                      "number": 456,
                      "state": "open",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/456",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456"]:
                return _Completed(
                    args,
                    f"""{{
                      "body": "<!-- robert-workstream\\norigin_workstream_id: github:example/backend#123\\nsource_issue: 123\\ntask_id: {issue_task_id}\\ncreated_by: robert\\n-->\\nOpened PR",
                      "head": {{"ref": "codex/dd-123-task"}},
                      "user": {{"login": "robert-bot"}}
                    }}""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456/comments"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456/reviews"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "review-comment-456",
                        "body": "@robert-bot The compatibility path is still wrong; please update this branch.",
                        "created_at": "2026-06-22T13:00:01Z",
                        "author_association": "MEMBER",
                        "user": {"login": "reviewer"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        pr_followup = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_pr_runner,
        )

        self.assertTrue(pr_followup["ok"], pr_followup)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            workstreams = conn.execute(
                """
                SELECT workstream_id, origin_workstream_id, lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id IN (
                  'github:example/backend#123',
                  'github:example/backend!456'
                )
                ORDER BY workstream_id
                """
            ).fetchall()
            tasks = conn.execute(
                """
                SELECT t.task_id, t.workstream_id, t.parent_task_id, t.route_id,
                       t.expected_output, a.branch_name
                FROM tasks t
                LEFT JOIN attempts a ON a.task_id = t.task_id
                ORDER BY t.created_at, t.task_id
                """
            ).fetchall()
            event_relationships = conn.execute(
                """
                SELECT t.workstream_id, te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN tasks t ON t.task_id = te.task_id
                JOIN github_events ge ON ge.event_id = te.event_id
                ORDER BY t.workstream_id, ge.event_fingerprint
                """
            ).fetchall()

        self.assertEqual(
            workstreams,
            [
                (
                    "github:example/backend!456",
                    "github:example/backend#123",
                    "active",
                    tasks[1][0],
                ),
                (
                    "github:example/backend#123",
                    None,
                    "completed",
                    None,
                ),
            ],
        )
        self.assertEqual(tasks[0][1], "github:example/backend#123")
        self.assertIsNone(tasks[0][2])
        self.assertEqual(tasks[0][3:5], ("new-pr", "new_pr"))
        self.assertEqual(tasks[1][1], "github:example/backend!456")
        self.assertIsNone(tasks[1][2])
        self.assertEqual(
            tasks[1][3:6],
            ("update-existing-pr", "update_existing_pr", "codex/dd-123-task"),
        )
        self.assertEqual(
            event_relationships,
            [
                (
                    "github:example/backend!456",
                    "trigger",
                    "review_comment:review-comment-456",
                ),
                (
                    "github:example/backend#123",
                    "consumed",
                    "comment:comment-1",
                ),
            ],
        )

    def test_active_issue_task_is_canceled_when_remote_issue_closes(self):
        from robert_agent import run_once
        def fake_open_issue_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(
                    args,
                    """[
                      [
                        {
                          "id": "notification-issue-123",
                          "updated_at": "2026-06-22T12:00:00Z",
                          "reason": "mention",
                          "subject": {
                            "type": "Issue",
                            "url": "https://api.github.com/repos/example/backend/issues/123"
                          },
                          "repository": {
                            "full_name": "example/backend"
                          }
                        }
                      ]
                    ]""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(
                    args,
                    """{
                      "number": 123,
                      "state": "open",
                      "title": "Compatibility bug",
                      "updated_at": "2026-06-22T12:00:00Z",
                      "html_url": "https://github.com/example/backend/issues/123",
                      "user": {"login": "wklken"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123/timeline"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123/comments"]:
                return _Completed(
                    args,
                    """[
                      {
                        "id": "comment-1",
                        "body": "@robert-bot please fix this bug",
                        "created_at": "2026-06-22T12:00:01Z",
                        "author_association": "OWNER",
                        "user": {"login": "wklken"}
                      }
                    ]""",
                )
            raise AssertionError(args)

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_open_issue_runner,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

        def fake_closed_issue_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/123"]:
                return _Completed(
                    args,
                    """{
                      "number": 123,
                      "state": "closed",
                      "state_reason": "not_planned",
                      "title": "Compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/issues/123",
                      "user": {"login": "wklken"}
                    }""",
                )
            raise AssertionError(args)

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_closed_issue_runner,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            workstream = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend#123'
                """
            ).fetchone()
            source = conn.execute(
                """
                SELECT state, metadata_json
                FROM github_sources
                WHERE source_key = 'github:example/backend#123'
                """
            ).fetchone()
            audit = conn.execute(
                """
                SELECT event_type, payload_json
                FROM audit_events
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(task, ("canceled",))
        self.assertEqual(attempt, ("canceled",))
        self.assertEqual(workstream, ("canceled", None))
        self.assertEqual(source[0], "closed")
        self.assertEqual(json.loads(source[1])["remote_state"]["state_reason"], "not_planned")
        self.assertEqual(audit[0], "remote_source_closed")
        self.assertEqual(json.loads(audit[1])["source_key"], "github:example/backend#123")

    def test_active_pr_task_is_canceled_when_remote_pr_is_merged(self):
        from robert_agent import run_once
        db_path, task_id, attempt_id = self._seed_active_pr_task(456)

        def fake_merged_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/456"]:
                return _Completed(
                    args,
                    """{
                      "number": 456,
                      "state": "closed",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/456",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/456"]:
                return _Completed(
                    args,
                    """{
                      "number": 456,
                      "state": "closed",
                      "merged": true,
                      "merged_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/456"
                    }""",
                )
            raise AssertionError(args)

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_merged_pr_runner,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            workstream = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!456'
                """
            ).fetchone()
            source_metadata = json.loads(
                conn.execute(
                    """
                    SELECT metadata_json
                    FROM github_sources
                    WHERE source_key = 'github:example/backend!456'
                    """
                ).fetchone()[0]
            )
            audit = conn.execute(
                """
                SELECT event_type, payload_json
                FROM audit_events
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(task, ("canceled",))
        self.assertEqual(attempt, ("canceled",))
        self.assertEqual(workstream, ("completed", None))
        self.assertTrue(source_metadata["remote_state"]["merged"])
        self.assertEqual(audit[0], "remote_pr_merged")
        self.assertEqual(json.loads(audit[1])["source_key"], "github:example/backend!456")

    def test_completed_pr_workstream_marks_review_item_done_when_remote_pr_is_merged(self):
        from robert_agent import run_once
        from robert_agent import board, work_items

        db_path, task_id, attempt_id = self._seed_active_pr_task(459)
        now = "2026-06-22T12:00:00+00:00"
        issue_workstream_id = "github:example/backend#123"
        pr_workstream_id = "github:example/backend!459"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  html_url, title, state, author_login
                )
                VALUES (
                  'source:github:example/backend#123',
                  'repo:example/backend',
                  ?, 'issue', 123,
                  'https://github.com/example/backend/issues/123',
                  'Compatibility bug', 'open', 'wklken'
                )
                """,
                (issue_workstream_id,),
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  active_task_id, created_at, updated_at
                )
                VALUES (
                  ?, 'repo:example/backend',
                  'source:github:example/backend#123',
                  'completed', NULL, ?, ?
                )
                """,
                (issue_workstream_id, now, now),
            )
            item = work_items.ensure_github_work_item(
                conn,
                repo_id="repo:example/backend",
                source_id="source:github:example/backend#123",
                workstream_id=issue_workstream_id,
                actor_identity="wklken",
                route_confidence="high",
                now=now,
            )
            work_items.record_system_event(
                conn,
                item["work_item_id"],
                event_type="pr_opened",
                idempotency_key="pr-opened:459",
                metadata={"source_key": pr_workstream_id, "pr_number": 459},
                now=now,
            )
            conn.execute(
                "UPDATE tasks SET lifecycle = 'completed', updated_at = ? WHERE task_id = ?",
                (now, task_id),
            )
            conn.execute(
                "UPDATE attempts SET status = 'completed', finished_at = ? WHERE attempt_id = ?",
                (now, attempt_id),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'completed', active_task_id = NULL, updated_at = ?
                WHERE workstream_id = ?
                """,
                (now, pr_workstream_id),
            )

        self.assertEqual(
            board.get_work_item_detail(db_path, item["work_item_id"])["column"],
            "review",
        )

        def fake_merged_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/459"]:
                return _Completed(
                    args,
                    """{
                      "number": 459,
                      "state": "closed",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/459",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/459"]:
                return _Completed(
                    args,
                    """{
                      "number": 459,
                      "state": "closed",
                      "merged": true,
                      "merged_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/459"
                    }""",
                )
            raise AssertionError(args)

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_merged_pr_runner,
        )

        self.assertTrue(result["ok"], result)
        detail = board.get_work_item_detail(db_path, item["work_item_id"])
        self.assertEqual(detail["column"], "done")
        self.assertIn("pr_merged", [event["event_type"] for event in detail["events"]])

    def test_active_pr_task_is_canceled_when_remote_pr_is_closed_unmerged(self):
        from robert_agent import run_once
        db_path, task_id, attempt_id = self._seed_active_pr_task(457)

        def fake_closed_pr_runner(args, **_kwargs):
            if args[:3] == ["gh", "search", "issues"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "notifications"]:
                return _Completed(args, "[]")
            if args[:3] == ["gh", "api", "repos/example/backend/issues/457"]:
                return _Completed(
                    args,
                    """{
                      "number": 457,
                      "state": "closed",
                      "title": "Fix compatibility bug",
                      "updated_at": "2026-06-22T13:00:00Z",
                      "closed_at": "2026-06-22T13:00:00Z",
                      "html_url": "https://github.com/example/backend/pull/457",
                      "user": {"login": "robert-bot"}
                    }""",
                )
            if args[:3] == ["gh", "api", "repos/example/backend/pulls/457"]:
                return _Completed(
                    args,
                    """{
                      "number": 457,
                      "state": "closed",
                      "merged": false,
                      "merged_at": null,
                      "html_url": "https://github.com/example/backend/pull/457"
                    }""",
                )
            raise AssertionError(args)

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            discovery_runner=fake_closed_pr_runner,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            workstream = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!457'
                """
            ).fetchone()
            source_metadata = json.loads(
                conn.execute(
                    """
                    SELECT metadata_json
                    FROM github_sources
                    WHERE source_key = 'github:example/backend!457'
                    """
                ).fetchone()[0]
            )
            audit = conn.execute(
                """
                SELECT event_type, payload_json
                FROM audit_events
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        self.assertEqual(task, ("canceled",))
        self.assertEqual(attempt, ("canceled",))
        self.assertEqual(workstream, ("canceled", None))
        self.assertFalse(source_metadata["remote_state"]["merged"])
        self.assertEqual(audit[0], "remote_pr_closed")
        self.assertEqual(json.loads(audit[1])["source_key"], "github:example/backend!457")

    def test_closed_issue_source_is_skipped_before_dispatch(self):
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "closed-issue-comment",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please handle this closed issue",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/issues/123",
                            "state": "closed",
                            "state_reason": "completed",
                            "closed_at": "2026-06-22T13:00:00Z",
                            "source_updated_at": "2026-06-22T13:00:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["prompt_paths"], [])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            attempt_count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            source_state, source_metadata_json = conn.execute(
                """
                SELECT state, metadata_json
                FROM github_sources
                WHERE source_key = 'github:example/backend#123'
                """
            ).fetchone()
            event_status = conn.execute(
                """
                SELECT authorization_status
                FROM github_events
                WHERE event_fingerprint = 'comment:closed-issue-comment'
                """
            ).fetchone()[0]
            audit = conn.execute(
                "SELECT event_type, payload_json FROM audit_events"
            ).fetchone()
            route_output = json.loads(
                conn.execute(
                    """
                    SELECT output_json
                    FROM run_steps
                    WHERE step_key = 'route'
                    """
                ).fetchone()[0]
            )
        source_metadata = json.loads(source_metadata_json)
        audit_payload = json.loads(audit[1])
        self.assertEqual(task_count, 0)
        self.assertEqual(attempt_count, 0)
        self.assertEqual(source_state, "closed")
        self.assertEqual(event_status, "ignored_current_state_closed")
        self.assertEqual(audit[0], "current_state_closed")
        self.assertEqual(audit_payload["terminal_reason"], "remote_source_closed")
        self.assertEqual(audit_payload["skip_reason"], "source_already_closed_before_dispatch")
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["terminal_reason"],
            "remote_source_closed",
        )
        self.assertEqual(route_output["current_state_closed_skip_count"], 1)

    def test_merged_pr_source_is_skipped_before_dispatch(self):
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "merged-pr-comment",
                            "number": 456,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please handle this merged PR",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/pull/456",
                            "state": "closed",
                            "merged": True,
                            "merged_at": "2026-06-22T13:00:00Z",
                            "closed_at": "2026-06-22T13:00:00Z",
                            "source_updated_at": "2026-06-22T13:00:00Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["prompt_paths"], [])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            source_metadata = json.loads(
                conn.execute(
                    """
                    SELECT metadata_json
                    FROM github_sources
                    WHERE source_key = 'github:example/backend!456'
                    """
                ).fetchone()[0]
            )
            audit_payload = json.loads(
                conn.execute("SELECT payload_json FROM audit_events").fetchone()[0]
            )
        self.assertEqual(task_count, 0)
        self.assertEqual(audit_payload["terminal_reason"], "remote_pr_merged")
        self.assertTrue(audit_payload["remote_state"]["merged"])
        self.assertEqual(
            source_metadata["current_state_reconciliation"]["terminal_reason"],
            "remote_pr_merged",
        )

    def test_closed_pr_followup_for_active_workstream_is_not_skipped(self):
        from robert_agent import run_once
        db_path, task_id, _attempt_id = self._seed_active_pr_task(458)
        followup_fixture = self.root / "closed-pr-followup.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "closed-pr-followup",
                            "number": 458,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot handle the explicit follow-up",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/pull/458",
                            "state": "closed",
                            "merged": True,
                            "merged_at": "2026-06-22T13:00:00Z",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/dd-458-task",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#123",
                                    "origin_workstream_id": "github:example/backend#123",
                                    "source_issue": "123",
                                }
                            },
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            relationships = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.created_at, ge.event_fingerprint
                """,
                (task_id,),
            ).fetchall()
            current_state_audits = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'current_state_closed'"
            ).fetchone()[0]
        self.assertIn(("context", "comment:closed-pr-followup"), relationships)
        self.assertEqual(current_state_audits, 0)

    def test_storage_backfills_legacy_pr_task_into_pr_workstream(self):
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root)
                VALUES ('repo:example/backend', 'example/backend', 'robert-bot', 'master', '/tmp/repo', '/tmp/repo/.worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend#123', 'repo:example/backend', 'github:example/backend#123', 'issue', 123, '', 'open', 'wklken')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number, title, state, author_login)
                VALUES ('source:github:example/backend!456', 'repo:example/backend', 'github:example/backend!456', 'pull_request', 456, '', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend#123', 'repo:example/backend', 'source:github:example/backend#123', 'active', 'task-pr-followup', '2026-06-17T00:00:00+00:00', '2026-06-17T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO github_events(event_id, repo_id, source_id, event_fingerprint, event_type, actor_login, author_association, authorization_status, event_at, payload_json)
                VALUES ('event:comment:legacy', 'repo:example/backend', 'source:github:example/backend!456', 'comment:legacy', 'comment', 'wklken', 'COLLABORATOR', 'accepted_context', '2026-06-17T00:00:00Z', '{"workstream_id": "github:example/backend!456", "origin_workstream_id": "github:example/backend#123"}')
                """
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES ('task-pr-followup', 'github:example/backend#123', 'queued', 'P1', 'update-existing-pr', 'update_existing_pr', '2026-06-17T00:00:00+00:00', '2026-06-17T00:00:00+00:00')
                """
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, event_id, relationship, created_at)
                VALUES ('task-pr-followup', 'event:comment:legacy', 'trigger', '2026-06-17T00:00:00+00:00')
                """
            )

        storage.init_database(db_path)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                "SELECT workstream_id FROM tasks WHERE task_id = 'task-pr-followup'"
            ).fetchone()
        self.assertEqual(row, ("github:example/backend!456",))

    def test_unconsumed_pending_event_creates_child_task_after_parent_result(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
                            "actor_login": "reviewer",
                            "author_association": "MEMBER",
                            "body": "@robert-bot another point",
                        },
                        {
                            "id": "comment-3",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "reviewer",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot one more point",
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
                "planned_github_actions": _new_pr_actions(task_id=parent_task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "done",
                "used_skills": ["fast-small-pr"],
            },
        )
        self.assertTrue(record["ok"], record)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE github_actions SET publish_status = 'published' WHERE result_id = ?",
                (record["result_id"],),
            )

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            tasks = conn.execute(
                "SELECT task_id, parent_task_id, lifecycle FROM tasks ORDER BY created_at"
            ).fetchall()
            active_task_id = conn.execute(
                "SELECT active_task_id FROM workstreams"
            ).fetchone()[0]
            child_events = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.relationship, ge.event_fingerprint
                """,
                (tasks[1][0],),
            ).fetchall()
            prompt_path = Path(
                conn.execute(
                    "SELECT path FROM artifacts WHERE task_id = ? AND artifact_type = 'prompt'",
                    (tasks[1][0],),
                ).fetchone()[0]
            )
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0], (parent_task_id, None, "completed"))
        self.assertEqual(tasks[1][1], parent_task_id)
        self.assertEqual(tasks[1][2], "queued")
        self.assertEqual(active_task_id, tasks[1][0])
        self.assertEqual(
            child_events,
            [("context", "comment:comment-3"), ("trigger", "comment:comment-2")],
        )
        child_prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("comment:comment-2", child_prompt)
        self.assertIn("comment:comment-3", child_prompt)

    def test_project_memory_is_recorded_and_recalled_in_followup_prompt(self):
        from robert_agent.worker import result
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()

        record = result.record_result(
            db_path,
            {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "output_type": "new_pr",
                "planned_github_actions": _new_pr_actions(task_id=task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "opened PR",
                "used_skills": ["fast-small-pr"],
                "memory_delta": {
                    "status": "has_memory",
                    "entries": [
                        {
                            "operation": "upsert",
                            "kind": "decision",
                            "title": "DD PR follow-up uses update-existing-pr",
                            "short_summary": "When dd-pr-followup review comments arrive, update the PR branch rather than reopening the origin issue task.",
                            "long_summary": "The PR workstream is the active implementation thread for review comments on a DD-created PR.",
                            "paths": ["src/robert_agent/run_once.py"],
                            "symbols": ["_is_dd_pr_followup_event"],
                            "keywords": ["dd-pr-followup", "update-existing-pr"],
                            "confidence": "medium",
                        }
                    ],
                },
            },
        )
        self.assertTrue(record["ok"], record)

        audited = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(audited["ok"], audited)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            memory_count = conn.execute(
                "SELECT COUNT(*) FROM project_memory_entries"
            ).fetchone()[0]
            result_metadata = json.loads(
                conn.execute(
                    "SELECT metadata_json FROM worker_results WHERE result_id = ?",
                    (record["result_id"],),
                ).fetchone()[0]
            )
        self.assertEqual(memory_count, 1)
        self.assertEqual(result_metadata["project_memory"]["status"], "recorded")

        followup_fixture = self.root / "followup-memory.json"
        followup_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-memory-followup",
                            "number": 124,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please fix this dd-pr-followup handling",
                            "intent": "bug_fix",
                            "url": "https://github.com/example/backend/issues/124",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        recalled = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=followup_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(recalled["ok"], recalled)
        prompt = Path(recalled["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn("Relevant Project Memories", prompt)
        self.assertIn("DD PR follow-up uses update-existing-pr", prompt)
        self.assertIn("dd-pr-followup review comments", prompt)

    def test_approved_runtime_knowledge_is_injected_but_pending_candidate_is_not(self):
        from robert_agent import run_once
        db_path = self.data_dir / "dd.sqlite3"
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO knowledge_candidates(
                  candidate_id, repo_id, title, summary, prompt_text,
                  candidate_type, source_memory_ids_json, evidence_json,
                  confidence, status, created_at, metadata_json
                )
                VALUES (
                  'kc-pending', 'repo:example/backend',
                  'Pending knowledge must not appear',
                  'Pending candidates are proposals only.',
                  'PENDING KNOWLEDGE SHOULD NOT BE IN PROMPT',
                  'rule', '[]', '[]', 'medium', 'pending', ?, '{}'
                )
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO knowledge_candidates(
                  candidate_id, repo_id, title, summary, prompt_text,
                  candidate_type, source_memory_ids_json, evidence_json,
                  confidence, status, reviewed_at, reviewer, created_at, metadata_json
                )
                VALUES (
                  'kc-approved', 'repo:example/backend',
                  'Approved DD PR review rule',
                  'Approved candidates can enter worker prompts.',
                  'Use update-existing-pr for approved DD PR review follow-up handling.',
                  'rule', '[]', '[]', 'medium', 'approved', ?, 'wklken', ?, '{}'
                )
                """,
                (now, now),
            )
            conn.execute(
                """
                INSERT INTO runtime_knowledge(
                  knowledge_id, candidate_id, repo_id, scope_type, scope_value,
                  title, prompt_text, retrieval_boost_json, active,
                  approved_by, approved_at, created_at, updated_at, metadata_json
                )
                VALUES (
                  'rk-approved', 'kc-approved', 'repo:example/backend',
                  'route', 'new-pr',
                  'Approved DD PR review rule',
                  'Use update-existing-pr for approved DD PR review follow-up handling.',
                  '{"keywords": ["dd-pr-followup"]}', 1,
                  'wklken', ?, ?, ?, '{}'
                )
                """,
                (now, now, now),
            )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        prompt = Path(result["prompt_paths"][0]).read_text(encoding="utf-8")
        self.assertIn("Approved Runtime Knowledge", prompt)
        self.assertIn("Approved DD PR review rule", prompt)
        self.assertIn("Use update-existing-pr", prompt)
        self.assertNotIn("PENDING KNOWLEDGE SHOULD NOT BE IN PROMPT", prompt)

    def test_run_once_acquires_and_releases_agent_lease(self):
        from robert_agent import run_once
        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            lease = conn.execute(
                "SELECT resource_type, resource_key, status FROM leases"
            ).fetchone()
        self.assertEqual(
            lease,
            ("agent_run", "repo:example/backend", "released"),
        )

    def test_worktree_failure_during_route_fails_run_and_releases_lease(self):
        from robert_agent import run_once
        original_prepare_worktree = run_once._prepare_worktree

        def fake_prepare_worktree(_repo, _event, _route_result, _dry_run):
            raise subprocess.CalledProcessError(128, ["git", "fetch", "upstream", "master"])

        try:
            run_once._prepare_worktree = fake_prepare_worktree
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once._prepare_worktree = original_prepare_worktree

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_route")
        self.assertIn("git fetch upstream master", result["safe_error"])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            run_status, error_json = conn.execute(
                "SELECT status, error_json FROM agent_runs"
            ).fetchone()
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
            route_step = conn.execute(
                "SELECT status FROM run_steps WHERE step_key = 'route'"
            ).fetchone()[0]
        self.assertEqual(run_status, "failed")
        self.assertEqual(json.loads(error_json)["status"], "failed_route")
        self.assertEqual(lease_status, "released")
        self.assertEqual(route_step, "failed")

    def test_malformed_discovery_event_fails_run_and_releases_lease(self):
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "number": 123,
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

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "failed_discovery")
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            run_status, error_json = conn.execute(
                "SELECT status, error_json FROM agent_runs"
            ).fetchone()
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
            discover_step = conn.execute(
                "SELECT status FROM run_steps WHERE step_key = 'discover'"
            ).fetchone()[0]
        self.assertEqual(run_status, "failed")
        self.assertIn("event id", json.loads(error_json)["safe_error"])
        self.assertEqual(lease_status, "released")
        self.assertEqual(discover_step, "failed")

    def test_publish_exception_fails_run_and_releases_lease(self):
        from robert_agent import run_once
        original_publish = run_once.publish.publish_ready_actions

        def fake_publish_ready_actions(_db_path, dry_run=False, repo_id=None):
            raise FileNotFoundError("gh")

        try:
            run_once.publish.publish_ready_actions = fake_publish_ready_actions
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "publish_failed")
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            run_status, error_json = conn.execute(
                "SELECT status, error_json FROM agent_runs"
            ).fetchone()
            lease_status = conn.execute("SELECT status FROM leases").fetchone()[0]
            publish_step = conn.execute(
                "SELECT status FROM run_steps WHERE step_key = 'publish_actions'"
            ).fetchone()[0]
        self.assertEqual(run_status, "failed")
        self.assertEqual(json.loads(error_json)["status"], "publish_failed")
        self.assertEqual(lease_status, "released")
        self.assertEqual(publish_step, "failed")

    def test_supervise_marks_stale_running_attempt_and_notifies(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'running', heartbeat_at = ?, started_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-stale', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "stale")
        self.assertEqual(notification, ("worker_stale", "recorded"))

    def test_supervise_fails_running_attempt_without_startup_snapshot(self):
        from robert_agent import run_once
        config_text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            config_text.replace(
                "hard_timeout_minutes: 90",
                "hard_timeout_minutes: 90\nworker_startup_grace_seconds: 30",
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'running', heartbeat_at = ?, started_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream_lifecycle = conn.execute(
                "SELECT lifecycle FROM workstreams"
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertIn("failed_worker_startup", failure_json)
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(workstream_lifecycle, "failed")
        self.assertEqual(notification, ("worker_startup_failed", "recorded"))

    def test_supervise_fails_running_attempt_when_dispatch_pid_is_gone(self):
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    now,
                    now,
                    json.dumps({"dispatch": {"pid": 987654, "status": "running"}}),
                    attempt_id,
                ),
            )

        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            self.assertEqual(pid, 987654)
            if sig == 0:
                raise ProcessLookupError()
            raise ProcessLookupError()

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertIn("failed_worker_process_exited", failure_json)
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(notification, ("worker_process_exited", "recorded"))

    def test_supervise_prepares_resume_attempt_when_pid_is_gone_after_progress(self):
        from robert_agent import run_once
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
        stdout_path = self.data_dir / "artifacts" / "attempt.stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            "I'm waiting on the background test run before I record the PR result.\n"
            "Command exited 0\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id, worktree_path, branch_name = conn.execute(
                "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    now,
                    now,
                    json.dumps({"dispatch": {"pid": 987654, "status": "running"}}),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-command-done', ?, 'verify', 'completed', 'Command exited 0', 'Record result', ?)
                """,
                (attempt_id, now),
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, created_at
                )
                VALUES ('artifact-worker-stdout', ?, ?, 'worker_stdout', ?, ?)
                """,
                (task_id, attempt_id, str(stdout_path), now),
            )

        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            self.assertEqual(pid, 987654)
            if sig == 0:
                raise ProcessLookupError()
            raise ProcessLookupError()

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempts = conn.execute(
                """
                SELECT attempt_id, attempt_no, status, worktree_path, branch_name, metadata_json
                FROM attempts
                ORDER BY attempt_no
                """
            ).fetchall()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            artifacts = conn.execute(
                """
                SELECT artifact_type, path
                FROM artifacts
                WHERE task_id = ?
                ORDER BY created_at, artifact_type
                """,
                (task_id,),
            ).fetchall()
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempt_id)
        self.assertEqual(attempts[0][2], "failed")
        self.assertEqual(attempts[1][1:5], (2, "prepared", worktree_path, branch_name))
        resume_metadata = json.loads(attempts[1][5])["resume"]
        self.assertEqual(resume_metadata["previous_attempt_id"], attempt_id)
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(notification, ("worker_resume_prepared", "recorded"))
        artifact_types = [row[0] for row in artifacts]
        self.assertIn("recovery_context", artifact_types)
        prompt_path = Path([row[1] for row in artifacts if row[0] == "prompt"][-1])
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Resume Previous Attempt", prompt)
        self.assertIn(attempt_id, prompt)
        self.assertIn("Command exited 0", prompt)
        self.assertIn("git_status_short", prompt)

    def test_supervise_prepares_resume_attempt_when_timeout_pid_is_gone_after_progress(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        old = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
        stdout_path = self.data_dir / "artifacts" / "attempt-timeout.stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            "I have local changes and the verification command finished.\n"
            "Command exited 0\n",
            encoding="utf-8",
        )
        missing_pid = 987654
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id, worktree_path, branch_name = conn.execute(
                "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', started_at = ?, heartbeat_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    json.dumps(
                        {"dispatch": {"pid": missing_pid, "status": "running"}},
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES (
                  'phase-timeout-command-done', ?, 'verify', 'completed',
                  'Command exited 0', 'Record result', ?
                )
                """,
                (attempt_id, old),
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, created_at
                )
                VALUES ('artifact-timeout-worker-stdout', ?, ?, 'worker_stdout', ?, ?)
                """,
                (task_id, attempt_id, str(stdout_path), old),
            )

        signals = []
        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            self.assertEqual(pid, missing_pid)
            signals.append((pid, sig))
            raise ProcessLookupError()

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        self.assertEqual(signals, [(missing_pid, 0)])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempts = conn.execute(
                """
                SELECT
                  attempt_id, attempt_no, status, worktree_path, branch_name,
                  metadata_json, failure_json
                FROM attempts
                ORDER BY attempt_no
                """
            ).fetchall()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempt_id)
        self.assertEqual(attempts[0][2], "failed")
        failure = json.loads(attempts[0][6])
        self.assertEqual(failure["status"], "needs_resume")
        self.assertEqual(failure["original_status"], "failed_timeout")
        self.assertEqual(attempts[1][1:5], (2, "prepared", worktree_path, branch_name))
        resume_metadata = json.loads(attempts[1][5])["resume"]
        self.assertEqual(resume_metadata["previous_attempt_id"], attempt_id)
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(notification, ("worker_resume_prepared", "recorded"))

    def test_supervise_recovers_failed_timeout_attempt_when_pid_is_gone_after_progress(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        old = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
        stdout_path = self.data_dir / "artifacts" / "failed-timeout.stdout.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(
            "The worker had local changes before timing out.\n"
            "Command exited 0\n",
            encoding="utf-8",
        )
        missing_pid = 987654
        failure = {
            "ok": False,
            "status": "failed_timeout",
            "terminate": True,
            "heartbeat_age_minutes": 95,
            "runtime_minutes": 95,
        }
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id, worktree_path, branch_name = conn.execute(
                "SELECT task_id, attempt_id, worktree_path, branch_name FROM attempts"
            ).fetchone()
            workstream_id = conn.execute(
                "SELECT workstream_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE tasks SET lifecycle = 'failed', updated_at = ? WHERE task_id = ?",
                (old, task_id),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'failed', started_at = ?, heartbeat_at = ?,
                    finished_at = ?, failure_json = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    old,
                    json.dumps(failure, sort_keys=True),
                    json.dumps(
                        {"dispatch": {"pid": missing_pid, "status": "running"}},
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                UPDATE workstreams
                SET lifecycle = 'failed', active_task_id = NULL, updated_at = ?
                WHERE workstream_id = ?
                """,
                (old, workstream_id),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES (
                  'phase-failed-timeout-command-done', ?, 'verify', 'completed',
                  'Command exited 0', 'Record result', ?
                )
                """,
                (attempt_id, old),
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, task_id, attempt_id, artifact_type, path, created_at
                )
                VALUES ('artifact-failed-timeout-worker-stdout', ?, ?, 'worker_stdout', ?, ?)
                """,
                (task_id, attempt_id, str(stdout_path), old),
            )

        signals = []
        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            self.assertEqual(pid, missing_pid)
            signals.append((pid, sig))
            raise ProcessLookupError()

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        self.assertEqual(signals, [(missing_pid, 0)])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempts = conn.execute(
                """
                SELECT
                  attempt_id, attempt_no, status, worktree_path, branch_name,
                  metadata_json, failure_json
                FROM attempts
                ORDER BY attempt_no
                """
            ).fetchall()
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            supervise_output = conn.execute(
                """
                SELECT output_json
                FROM run_steps
                WHERE run_id = ? AND step_key = 'supervise'
                """,
                (second["run_id"],),
            ).fetchone()[0]

        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempt_id)
        self.assertEqual(attempts[0][2], "failed")
        updated_failure = json.loads(attempts[0][6])
        self.assertEqual(updated_failure["status"], "needs_resume")
        self.assertEqual(updated_failure["original_status"], "failed_timeout")
        self.assertEqual(attempts[1][1:5], (2, "prepared", worktree_path, branch_name))
        resume_metadata = json.loads(attempts[1][5])["resume"]
        self.assertEqual(resume_metadata["previous_attempt_id"], attempt_id)
        self.assertEqual(task_lifecycle, "running")
        self.assertEqual(workstream, ("active", task_id))
        self.assertEqual(notification, ("worker_resume_prepared", "recorded"))
        supervise = json.loads(supervise_output)
        self.assertEqual(supervise["failed_timeout_recovered_count"], 1)
        self.assertEqual(supervise["supervised_count"], 1)

    def test_supervise_waits_when_pid_is_gone_but_background_command_is_running(self):
        from robert_agent import run_once
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
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'running', started_at = ?, heartbeat_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    now,
                    now,
                    json.dumps({"dispatch": {"pid": 987654, "status": "running"}}),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-command-running', ?, 'verify', 'running', 'Command still running', 'Wait for command completion', ?)
                """,
                (attempt_id, now),
            )

        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            self.assertEqual(pid, 987654)
            if sig == 0:
                raise ProcessLookupError()
            raise ProcessLookupError()

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempts = conn.execute(
                "SELECT attempt_id, status, metadata_json FROM attempts ORDER BY attempt_no"
            ).fetchall()
            notification_count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][1], "running")
        supervise_status = json.loads(attempts[0][2])["supervise"]["status"]
        self.assertEqual(supervise_status, "orphaned_command_running")
        self.assertEqual(notification_count, 0)

    def test_dispatch_process_status_treats_zombie_pid_as_not_found(self):
        from robert_agent import run_once
        original_kill = run_once.os.kill
        original_read_text = run_once.Path.read_text

        def fake_kill(pid, sig):
            self.assertEqual(pid, 43210)
            self.assertEqual(sig, 0)

        def fake_read_text(path_obj, encoding="utf-8"):
            self.assertEqual(str(path_obj), "/proc/43210/stat")
            return "43210 (cbc) Z 1 2 3 4 5"

        try:
            run_once.os.kill = fake_kill
            run_once.Path.read_text = fake_read_text
            status = run_once._dispatch_process_status({"dispatch": {"pid": 43210}})
        finally:
            run_once.os.kill = original_kill
            run_once.Path.read_text = original_read_text

        self.assertEqual(
            status,
            {
                "status": "not_found",
                "pid": 43210,
                "reason": "zombie",
                "process_state": "Z",
            },
        )

    def test_terminate_attempt_process_skips_sigterm_for_zombie_pid(self):
        from robert_agent import run_once
        kill_calls = []
        original_kill = run_once.os.kill
        original_read_text = run_once.Path.read_text

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))

        def fake_read_text(path_obj, encoding="utf-8"):
            self.assertEqual(str(path_obj), "/proc/24680/stat")
            return "24680 (cbc) Z 1 2 3 4 5"

        try:
            run_once.os.kill = fake_kill
            run_once.Path.read_text = fake_read_text
            result = run_once._terminate_attempt_process({"dispatch": {"pid": 24680}})
        finally:
            run_once.os.kill = original_kill
            run_once.Path.read_text = original_read_text

        self.assertEqual(kill_calls, [])
        self.assertEqual(
            result,
            {
                "status": "not_found",
                "reason": "zombie",
                "pid": 24680,
                "process_state": "Z",
                "signal": "SIGTERM",
            },
        )

    def test_dispatch_process_status_detects_zombie_pid_without_proc_stat(self):
        from robert_agent import run_once
        original_kill = run_once.os.kill
        original_read_text = run_once.Path.read_text
        original_run = run_once.subprocess.run

        class Completed:
            returncode = 0
            stdout = "Z    \n"
            stderr = ""

        def fake_kill(pid, sig):
            self.assertEqual(pid, 13579)
            self.assertEqual(sig, 0)

        def fake_read_text(path_obj, encoding="utf-8"):
            raise FileNotFoundError(str(path_obj))

        def fake_run(command, **kwargs):
            self.assertEqual(command, ["ps", "-o", "stat=", "-p", "13579"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            return Completed()

        try:
            run_once.os.kill = fake_kill
            run_once.Path.read_text = fake_read_text
            run_once.subprocess.run = fake_run
            status = run_once._dispatch_process_status({"dispatch": {"pid": 13579}})
        finally:
            run_once.os.kill = original_kill
            run_once.Path.read_text = original_read_text
            run_once.subprocess.run = original_run

        self.assertEqual(
            status,
            {
                "status": "not_found",
                "pid": 13579,
                "reason": "zombie",
                "process_state": "Z",
            },
        )

    def test_supervise_keeps_started_worker_with_prepare_snapshot_running(self):
        from robert_agent import run_once
        config_text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(
            config_text.replace(
                "hard_timeout_minutes: 90",
                "hard_timeout_minutes: 90\nworker_startup_grace_seconds: 30",
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                "UPDATE attempts SET status = 'running', heartbeat_at = ?, started_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-prepare', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt_status, "running")
        self.assertIsNone(failure_json)

    def test_supervise_restores_recovered_stale_attempt(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', heartbeat_at = ?, started_at = ?
                WHERE attempt_id = ?
                """,
                (
                    now.isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-recovered', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, (now - timedelta(minutes=10)).isoformat()),
            )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        self.assertEqual(attempt_status, "running")

    def test_supervise_does_not_restore_stale_attempt_when_pid_is_gone(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        now = datetime.now(timezone.utc)
        missing_pid = 987654
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', heartbeat_at = ?, started_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    now.isoformat(),
                    (now - timedelta(minutes=10)).isoformat(),
                    json.dumps(
                        {"dispatch": {"pid": missing_pid, "status": "running"}},
                        sort_keys=True,
                    ),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-recovered', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, (now - timedelta(minutes=10)).isoformat()),
            )

        signals = []
        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            self.assertEqual(pid, missing_pid)
            signals.append((pid, sig))
            raise ProcessLookupError()

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=True,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        self.assertIn((missing_pid, 0), signals)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status, failure_json, metadata_json = conn.execute(
                "SELECT status, failure_json, metadata_json FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        failure = json.loads(failure_json)
        metadata = json.loads(metadata_json)
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(failure["status"], "failed_worker_process_exited")
        self.assertEqual(
            metadata["supervise"]["status"],
            "failed_worker_process_exited",
        )
        self.assertEqual(notification, ("worker_process_exited", "recorded"))

    def test_stale_attempt_later_hard_timeout_signals_pid_and_releases_workstream(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        old = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', heartbeat_at = ?, started_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    json.dumps({"dispatch": {"pid": 12345}}, sort_keys=True),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-timeout', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
            )

        signals = []
        original_kill = run_once.os.kill

        def fake_kill(pid, sig):
            signals.append((pid, sig))

        try:
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        self.assertEqual(signals, [(12345, 0), (12345, run_once.signal.SIGTERM)])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            notification = conn.execute(
                "SELECT notification_type, status FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(workstream, ("failed", None))
        self.assertEqual(notification, ("worker_timeout", "recorded"))

    def test_stale_attempt_does_not_consume_dispatch_capacity(self):
        from robert_agent import run_once
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', heartbeat_at = ?, started_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    json.dumps({"dispatch": {"pid": 12345}}, sort_keys=True),
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO worker_phases(
                  phase_id, attempt_id, phase, status, summary, next_step, created_at
                )
                VALUES ('phase-capacity', ?, 'prepare', 'running', 'worker started', '', ?)
                """,
                (attempt_id, old),
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
        signals = []
        original_dispatch = run_once.dispatch.dispatch_worker
        original_kill = run_once.os.kill

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 123, "command": ["cbc"]}

        def fake_kill(pid, sig):
            self.assertEqual(pid, 12345)
            signals.append((pid, sig))

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            run_once.os.kill = fake_kill
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch
            run_once.os.kill = original_kill

        self.assertTrue(second["ok"], second)
        self.assertEqual(signals, [(12345, 0)])
        self.assertEqual(len(calls), 1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            statuses = conn.execute(
                "SELECT status FROM attempts ORDER BY started_at"
            ).fetchall()
            task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertEqual(statuses, [("stale",), ("running",)])
        self.assertEqual(task_count, 2)

    def test_live_discovery_failure_preserves_supervision_state(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        old = (datetime.now(timezone.utc) - timedelta(minutes=95)).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id, attempt_id = conn.execute(
                "SELECT task_id, attempt_id FROM attempts"
            ).fetchone()
            conn.execute(
                "UPDATE tasks SET lifecycle = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute(
                """
                UPDATE attempts
                SET status = 'stale', heartbeat_at = ?, started_at = ?, metadata_json = ?
                WHERE attempt_id = ?
                """,
                (
                    old,
                    old,
                    json.dumps({"dispatch": {"pid": 12345}}, sort_keys=True),
                    attempt_id,
                ),
            )

        signals = []
        original_kill = run_once.os.kill
        original_collect_notifications = run_once.discover.collect_account_notifications
        original_collect = run_once.discover.collect_live_events

        def fake_kill(pid, sig):
            signals.append((pid, sig))

        def fake_collect_notifications(_repos, runner=None):
            return {}

        def fake_collect(
            _repo,
            known_workstreams=None,
            notification_hints=None,
            include_notifications=True,
            **_kwargs,
        ):
            self.assertEqual(notification_hints, [])
            self.assertFalse(include_notifications)
            raise subprocess.CalledProcessError(1, ["gh", "search", "issues"])

        try:
            run_once.os.kill = fake_kill
            run_once.discover.collect_account_notifications = fake_collect_notifications
            run_once.discover.collect_live_events = fake_collect
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=False,
            )
        finally:
            run_once.os.kill = original_kill
            run_once.discover.collect_account_notifications = original_collect_notifications
            run_once.discover.collect_live_events = original_collect

        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "failed_discovery")
        self.assertEqual(signals, [(12345, 0), (12345, run_once.signal.SIGTERM)])
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_status = conn.execute(
                "SELECT status FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            run_status = conn.execute(
                "SELECT status FROM agent_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
            notification = conn.execute(
                "SELECT notification_type, status, task_id FROM notifications"
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(run_status, "failed")
        self.assertEqual(notification, ("worker_timeout", "recorded", task_id))

    def test_live_run_dispatches_existing_prepared_attempt(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute(
                "UPDATE attempts SET started_at = ?, heartbeat_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )
        calls = []
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["dry_run"])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_status, started_at, heartbeat_at = conn.execute(
                "SELECT status, started_at, heartbeat_at FROM attempts"
            ).fetchone()
        self.assertEqual(attempt_status, "running")
        self.assertGreater(datetime.fromisoformat(started_at), datetime.fromisoformat(old))
        self.assertGreater(datetime.fromisoformat(heartbeat_at), datetime.fromisoformat(old))

    def test_live_run_prepares_missing_worktree_before_dispatching_prepared_attempt(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        replacement_worktree = self.worktree_root / "codex__dd-123-task"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            planned_worktree = Path(
                conn.execute("SELECT worktree_path FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()[0]
            )
        self.assertFalse(planned_worktree.exists())
        calls = []
        prepare_calls = []
        original_dispatch = run_once.dispatch.dispatch_worker
        original_prepare_worktree = run_once._prepare_worktree
        original_can_materialize_worktree = run_once._can_materialize_worktree

        def fake_prepare_worktree(repo, event, route_result, dry_run):
            prepare_calls.append(
                {
                    "dry_run": dry_run,
                    "event_fingerprint": event["event_fingerprint"],
                    "route_id": route_result["route_id"],
                }
            )
            self.assertFalse(dry_run)
            return {
                "worktree_path": str(replacement_worktree),
                "branch_name": "codex/dd-123-task",
            }

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            run_once._can_materialize_worktree = lambda _repo: True
            run_once._prepare_worktree = fake_prepare_worktree
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch
            run_once._prepare_worktree = original_prepare_worktree
            run_once._can_materialize_worktree = original_can_materialize_worktree

        self.assertTrue(second["ok"], second)
        self.assertEqual(
            prepare_calls,
            [
                {
                    "dry_run": False,
                    "event_fingerprint": "comment:comment-1",
                    "route_id": "new-pr",
                }
            ],
        )
        self.assertEqual(calls[0]["worktree_path"], str(replacement_worktree))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt = conn.execute(
                "SELECT status, worktree_path, branch_name FROM attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt, ("running", str(replacement_worktree), "codex/dd-123-task"))

    def test_live_run_passes_configured_worker_command_to_dispatch(self):
        from robert_agent import run_once
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\nworker_agent: codex\nworker_command: /opt/homebrew/bin/codex",
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
        old = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
            conn.execute(
                "UPDATE attempts SET started_at = ?, heartbeat_at = ? WHERE attempt_id = ?",
                (old, old, attempt_id),
            )
        calls = []
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": [kwargs["worker_command"], "-p"],
            }

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["worker_agent"], "codex")
        self.assertEqual(calls[0]["worker_command"], "/opt/homebrew/bin/codex")
        self.assertEqual(calls[0]["worker_agent_config"]["command_argv"], ["/opt/homebrew/bin/codex"])

    def test_classification_result_uses_selected_worker_defaults_and_route_override(self):
        from robert_agent import run_once
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                "database: dd.sqlite3\n"
                "workers:\n"
                "  - name: default\n"
                "    agent: cbc\n"
                "    command: cbc\n"
                "    default_model: gpt-5.4\n"
                "    default_effort: high\n"
                "  - name: classifier\n"
                "    agent: codex\n"
                "    command: /opt/homebrew/bin/codex\n"
                "    default_model: gpt-5.6-sol\n"
                "    default_effort: medium\n"
                "route_worker_models:\n"
                "  classification-result:\n"
                "    worker: classifier\n"
                "    effort: xhigh",
            ),
            encoding="utf-8",
        )
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "unclear-1",
                            "number": 123,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "body": "@robert-bot please classify this",
                            "intent": "unclear",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn:
            route_id, metadata_json = conn.execute(
                """
                SELECT t.route_id, a.metadata_json
                FROM attempts a
                JOIN tasks t ON t.task_id = a.task_id
                """
            ).fetchone()
        command = json.loads(metadata_json)["dispatch"]["command"]
        self.assertEqual(route_id, "classification-result")
        self.assertEqual(command[0], "/opt/homebrew/bin/codex")
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="xhigh"', command)

    def test_live_dispatch_commits_attempt_before_worker_can_record_progress(self):
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
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
        db_path = self.data_dir / "dd.sqlite3"
        visibility = []
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            with closing(sqlite3.connect(db_path)) as external:
                row = external.execute(
                    "SELECT status FROM attempts WHERE attempt_id = ?",
                    (kwargs["attempt_id"],),
                ).fetchone()
            visibility.append(row[0] if row else None)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertTrue(result["ok"], result)
        self.assertEqual(visibility, ["prepared"])

    def test_live_dispatch_does_not_overwrite_fast_worker_result(self):
        from robert_agent.worker import result as worker_result
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
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
        db_path = self.data_dir / "dd.sqlite3"
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            record = worker_result.record_result(
                db_path,
                {
                    "task_id": kwargs["task_id"],
                    "attempt_id": kwargs["attempt_id"],
                    "output_type": "comment_analysis",
                    "planned_github_actions": [
                        {
                            "type": "comment",
                            "target_url": "https://github.com/example/backend/issues/123",
                            "body": _dd_comment_body(
                                "Analysis is ready",
                                task_id=kwargs["task_id"],
                                attempt_id=kwargs["attempt_id"],
                                fingerprints="comment:comment-analysis",
                            ),
                        }
                    ],
                    "consumed_event_fingerprints": ["comment:comment-analysis"],
                    "verification": [_verification_evidence("python -B -m unittest")],
                    "handoff": "ready",
                    "used_skills": ["fast-code-path"],
                },
            )
            self.assertTrue(record["ok"], record)
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
            }

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            first = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertTrue(first["ok"], first)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
        self.assertEqual(attempt_status, "completed")
        self.assertEqual(result_count, 1)

        original_publish = run_once.publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE github_actions
                    SET publish_status = 'published',
                        external_id = '987',
                        target_url = 'https://github.com/example/backend/issues/123#issuecomment-987'
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            run_once.publish.publish_ready_actions = fake_publish_ready_actions
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute(
                "SELECT lifecycle FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            workstream = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            action_status = conn.execute(
                "SELECT audit_status, publish_status FROM github_actions WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            relationship = conn.execute(
                "SELECT relationship FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        self.assertEqual(task_lifecycle, "completed")
        self.assertEqual(workstream, ("completed", None))
        self.assertEqual(action_status, ("accepted", "published"))
        self.assertEqual(relationship, "consumed")

    def test_live_dispatch_accepts_fast_successful_worker_subprocess_result(self):
        import os
        from robert_agent import run_once
        self.fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-analysis",
                            "number": 123,
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
        worker_command = self.root / "mock-worker.py"
        worker_command.write_text(
            f"""#!/usr/bin/env python3
import json
import sys

sys.path.insert(0, {str(PACKAGE_ROOT.parent)!r})
from robert_agent.worker import result

prompt = sys.stdin.read()
fields = {{}}
for line in prompt.splitlines():
    if ": " in line:
        key, value = line.split(": ", 1)
        fields[key] = value
fingerprint_line = fields["event_fingerprints"].strip()
fingerprints = json.loads(fingerprint_line)
payload = {{
    "task_id": fields["task_id"],
    "attempt_id": fields["attempt_id"],
    "output_type": "comment_analysis",
    "planned_github_actions": [
        {{
            "type": "comment",
            "target_url": "https://github.com/example/backend/issues/123",
            "body": "<!-- robert-comment task_id={{}} attempt_id={{}} event_fingerprints={{}} -->\\nAnalysis is ready".format(
                fields["task_id"],
                fields["attempt_id"],
                ",".join(fingerprints),
            ),
        }}
    ],
    "consumed_event_fingerprints": fingerprints,
    "verification": [{_verification_evidence("mock-worker")}],
    "handoff": "ready",
    "used_skills": [],
}}
record = result.record_result(fields["db_path"], payload)
print(json.dumps(record, sort_keys=True))
raise SystemExit(0 if record["ok"] else 1)
""",
            encoding="utf-8",
        )
        os.chmod(worker_command, 0o755)
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "database: dd.sqlite3",
                f"database: dd.sqlite3\nworker_command: {worker_command}",
            ),
            encoding="utf-8",
        )

        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=False,
            skip_external=True,
        )

        self.assertTrue(first["ok"], first)
        db_path = self.data_dir / "dd.sqlite3"
        for _ in range(100):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
                result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            if attempt_status == "completed" and result_count == 1:
                break
            time.sleep(0.1)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_id = conn.execute("SELECT task_id FROM tasks").fetchone()[0]
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            attempt_status = conn.execute("SELECT status FROM attempts").fetchone()[0]
            result_count = conn.execute("SELECT COUNT(*) FROM worker_results").fetchone()[0]
            notification_count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        self.assertEqual(task_lifecycle, "queued")
        self.assertEqual(attempt_status, "completed")
        self.assertEqual(result_count, 1)
        self.assertEqual(notification_count, 0)

        original_publish = run_once.publish.publish_ready_actions

        def fake_publish_ready_actions(db_path, dry_run=False, repo_id=None):
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    UPDATE github_actions
                    SET publish_status = 'published',
                        external_id = '987',
                        target_url = 'https://github.com/example/backend/issues/123#issuecomment-987'
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
            return {
                "ok": True,
                "status": "published",
                "published_count": 1,
                "skipped_count": 0,
                "failed_count": 0,
            }

        try:
            run_once.publish.publish_ready_actions = fake_publish_ready_actions
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.publish.publish_ready_actions = original_publish

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            workstream = conn.execute("SELECT lifecycle, active_task_id FROM workstreams").fetchone()
        self.assertEqual(task_lifecycle, "completed")
        self.assertEqual(workstream, ("completed", None))

    def test_live_dispatch_records_worker_log_artifacts(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        stdout_path = self.data_dir / "artifacts" / "worker.stdout.log"
        stderr_path = self.data_dir / "artifacts" / "worker.stderr.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text("worker stdout", encoding="utf-8")
        stderr_path.write_text("worker stderr", encoding="utf-8")
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            return {
                "ok": True,
                "status": "running",
                "task_id": kwargs["task_id"],
                "attempt_id": kwargs["attempt_id"],
                "pid": 12345,
                "command": ["cbc", "-p"],
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
            }

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            rows = conn.execute(
                """
                SELECT artifact_type, path
                FROM artifacts
                WHERE artifact_type IN ('worker_stdout', 'worker_stderr')
                ORDER BY artifact_type
                """
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("worker_stderr", str(stderr_path)),
                ("worker_stdout", str(stdout_path)),
            ],
        )

    def test_live_dispatch_failure_records_failed_state(self):
        from robert_agent import run_once
        first = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=self.fixture_path,
            dry_run=True,
            skip_external=True,
        )
        self.assertTrue(first["ok"], first)
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**_kwargs):
            raise FileNotFoundError("cbc")

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertFalse(second["ok"], second)
        self.assertEqual(second["status"], "failed_dispatch")
        self.assertIn("cbc", second["safe_error"])
        with closing(sqlite3.connect(self.data_dir / "dd.sqlite3")) as conn, conn:
            attempt_status, failure_json = conn.execute(
                "SELECT status, failure_json FROM attempts"
            ).fetchone()
            task_lifecycle = conn.execute("SELECT lifecycle FROM tasks").fetchone()[0]
            workstream_lifecycle, active_task_id = conn.execute(
                "SELECT lifecycle, active_task_id FROM workstreams"
            ).fetchone()
            run_status, error_json = conn.execute(
                """
                SELECT status, error_json
                FROM agent_runs
                WHERE run_id = ?
                """,
                (second["run_id"],),
            ).fetchone()
            dispatch_step = conn.execute(
                """
                SELECT status
                FROM run_steps
                WHERE run_id = ? AND step_key = 'dispatch'
                """,
                (second["run_id"],),
            ).fetchone()
            summarize_step = conn.execute(
                """
                SELECT status
                FROM run_steps
                WHERE run_id = ? AND step_key = 'summarize'
                """,
                (second["run_id"],),
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, status
                FROM notifications
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(attempt_status, "failed")
        self.assertIn("failed_dispatch", failure_json)
        self.assertEqual(task_lifecycle, "failed")
        self.assertEqual(workstream_lifecycle, "failed")
        self.assertIsNone(active_task_id)
        self.assertEqual(run_status, "failed")
        self.assertIn("failed_dispatch", error_json)
        self.assertEqual(dispatch_step, ("failed",))
        self.assertEqual(summarize_step, ("skipped",))
        self.assertEqual(notification, ("worker_dispatch_failed", "recorded"))

    def test_failed_trigger_event_is_not_recreated_without_new_event(self):
        from robert_agent import run_once
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
        from robert_agent import run_once
        from robert_agent import storage

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
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-older', 'task-older', 1, 'prepared', ?, ?)
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
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at)
                VALUES ('attempt-pr', 'task-pr', 1, 'prepared', ?, 'codex/issue-2884-fix-operator', ?, ?)
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

        calls = []
        original_dispatch = run_once.dispatch.dispatch_worker

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
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

        self.assertTrue(result["ok"], result)
        self.assertEqual(calls[0][0], "task-pr")
        self.assertEqual(calls[0][1], "attempt-pr")

    def test_max_concurrency_blocks_new_dispatch_but_keeps_prepared_task(self):
        from robert_agent import run_once
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
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 123, "command": ["cbc"]}

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            second = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=self.fixture_path,
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

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
        from robert_agent.worker import result
        from robert_agent import run_once
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
                            "actor_login": "reviewer",
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
                "planned_github_actions": _new_pr_actions(task_id=parent_task_id),
                "consumed_event_fingerprints": ["comment:comment-1"],
                "verification": [_verification_evidence()],
                "handoff": "done",
                "used_skills": ["fast-small-pr"],
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
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at)
                VALUES ('attempt-busy', 'task-busy', 1, 'running', ?, ?)
                """,
                (now, now),
            )
        calls = []
        original_dispatch = run_once.dispatch.dispatch_worker

        def fake_dispatch_worker(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 456, "command": ["cbc"]}

        try:
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            audited = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch

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

    def test_old_dispatcher_and_runner_directories_are_removed(self):
        self.assertFalse((REPO_ROOT / "skills" / "dd-task-runner").exists())
        self.assertFalse((REPO_ROOT / "tests" / "dd_github_dispatcher").exists())

    def test_robot_authored_pr_followup_without_hidden_metadata_reuses_existing_branch(self):
        from robert_agent import run_once
        fixture_path = self.root / "pr-followup.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "comment-followup-1",
                            "number": 2883,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot 处理下\n\nRefs #2878",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2878-mcp-proxy",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#2878",
                                    "source_issue": "2878",
                                    "inferred_from": "pr_body_issue_reference",
                                }
                            },
                            "intent": "bug_fix",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT t.workstream_id, t.route_id, t.expected_output, a.branch_name, a.worktree_path
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                """
            ).fetchone()
        self.assertEqual(row[0], "github:example/backend!2883")
        self.assertEqual(row[1], "update-existing-pr")
        self.assertEqual(row[2], "update_existing_pr")
        self.assertEqual(row[3], "codex/issue-2878-mcp-proxy")
        self.assertIn("codex__issue-2878-mcp-proxy", row[4])

    def test_dd_pr_followup_trigger_is_prioritized_over_older_issue_workstream(self):
        from robert_agent import run_once
        fixture_path = self.root / "priority-followup.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "older-issue-task",
                            "number": 2878,
                            "source_type": "issue",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot please analyze this",
                            "intent": "analysis",
                            "event_at": "2026-06-17T02:30:00Z",
                        },
                        {
                            "id": "pr-followup-task",
                            "number": 2883,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot 处理下",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2878-mcp-proxy",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#2878",
                                    "source_issue": "2878",
                                    "inferred_from": "pr_body_issue_reference",
                                }
                            },
                            "event_at": "2026-06-17T02:38:44Z",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=fixture_path,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(result["ok"], result)
        db_path = self.data_dir / "dd.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT t.workstream_id, t.route_id, t.expected_output, a.branch_name
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                ORDER BY CASE WHEN t.workstream_id LIKE 'github:example/backend!%' THEN 0 ELSE 1 END,
                         t.created_at,
                         t.task_id
                """
            ).fetchall()
            pending = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                ORDER BY te.relationship, ge.event_fingerprint
                """
            ).fetchall()
        self.assertEqual(
            row,
            [
                (
                    "github:example/backend!2883",
                    "update-existing-pr",
                    "update_existing_pr",
                    "codex/issue-2878-mcp-proxy",
                ),
                (
                    "github:example/backend#2878",
                    "comment-analysis",
                    "comment_analysis",
                    None,
                ),
            ],
        )
        self.assertEqual(
            pending,
            [("trigger", "comment:older-issue-task"), ("trigger", "comment:pr-followup-task")],
        )

    def test_active_prepared_pr_task_is_rerouted_when_pr_followup_context_arrives(self):
        from robert_agent import run_once
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        task_id = "task-pr-2886"
        attempt_id = "attempt-pr-2886"
        prompt_path = self.data_dir / "artifacts" / task_id / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("stale prompt", encoding="utf-8")
        now = "2026-06-17T09:00:00+00:00"
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
                VALUES ('source:github:example/backend!2886', 'repo:example/backend', 'github:example/backend!2886', 'pull_request', 2886, 'fix(operator): improve release log context', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend!2886', 'repo:example/backend', 'source:github:example/backend!2886', 'github:example/backend#2884', 'active', ?, ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES (?, 'github:example/backend!2886', 'queued', 'P1', 'new-pr', 'new_pr', ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO route_decisions(route_decision_id, task_id, route_id, expected_output, allowed_github_actions_json, required_skills_json, confidence, created_at)
                VALUES ('route-pr-2886', ?, 'new-pr', 'new_pr', '["push_existing_pr", "open_pr", "comment"]', '["fast-small-pr"]', 'high', ?)
                """,
                (task_id, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at)
                VALUES (?, ?, 1, 'prepared', ?, 'codex/issue-2884-fix-operator', ?, ?)
                """,
                (
                    attempt_id,
                    task_id,
                    str(self.worktree_root / "issue-2884-fix-operator"),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at)
                VALUES ('artifact-pr-2886', ?, ?, 'prompt', ?, ?)
                """,
                (task_id, attempt_id, str(prompt_path), now),
            )

        pr_fixture = self.root / "pr-2886-followup.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-2886-followup",
                            "number": 2886,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot handle the major review items",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2884-fix-operator",
                            "pr_author_login": "robert-bot",
                            "metadata": {
                                "dd_workstream": {
                                    "workstream_id": "github:example/backend#2884",
                                    "origin_workstream_id": "github:example/backend#2884",
                                    "source_issue": "2884",
                                }
                            },
                            "workstream_id": "github:example/backend!2886",
                            "origin_workstream_id": "github:example/backend#2884",
                            "event_at": "2026-06-17T09:53:31Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        second = run_once.run_once(
            self.config_path,
            workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
            fixture_path=pr_fixture,
            dry_run=True,
            skip_external=True,
        )

        self.assertTrue(second["ok"], second)
        with closing(sqlite3.connect(db_path)) as conn, conn:
            row = conn.execute(
                """
                SELECT t.route_id, t.expected_output, a.branch_name
                FROM tasks t
                JOIN attempts a ON a.task_id = t.task_id
                WHERE t.workstream_id = 'github:example/backend!2886'
                """
            ).fetchone()
            prompt_path = conn.execute(
                """
                SELECT path
                FROM artifacts
                WHERE task_id = ? AND artifact_type = 'prompt'
                """
            , (task_id,)).fetchone()[0]
            relationships = conn.execute(
                """
                SELECT te.relationship, ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                ORDER BY te.relationship, ge.event_fingerprint
                """
            , (task_id,)).fetchall()
        self.assertEqual(row, ("update-existing-pr", "update_existing_pr", "codex/issue-2884-fix-operator"))
        prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.assertIn('expected_output: update_existing_pr', prompt)
        self.assertIn('push_existing_pr', prompt)
        self.assertIn('comment:pr-2886-followup', prompt)
        self.assertEqual(
            relationships,
            [("context", "comment:pr-2886-followup")],
        )

    def test_live_dispatch_uses_refreshed_worktree_after_prepared_pr_reroute(self):
        from robert_agent import run_once
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        task_id = "task-pr-2886"
        attempt_id = "attempt-pr-2886"
        prompt_path = self.data_dir / "artifacts" / task_id / "prompt.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text("stale prompt", encoding="utf-8")
        old_worktree_path = self.worktree_root / "dd-2884-task"
        new_worktree_path = self.worktree_root / "codex__issue-2884-fix-operator"
        now = "2026-06-17T09:00:00+00:00"
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
                VALUES ('source:github:example/backend!2886', 'repo:example/backend', 'github:example/backend!2886', 'pull_request', 2886, 'fix(operator): improve release log context', 'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, origin_workstream_id, lifecycle, active_task_id, created_at, updated_at)
                VALUES ('github:example/backend!2886', 'repo:example/backend', 'source:github:example/backend!2886', 'github:example/backend#2884', 'active', ?, ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, workstream_id, lifecycle, priority, route_id, expected_output, created_at, updated_at)
                VALUES (?, 'github:example/backend!2886', 'queued', 'P1', 'new-pr', 'new_pr', ?, ?)
                """,
                (task_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO route_decisions(route_decision_id, task_id, route_id, expected_output, allowed_github_actions_json, required_skills_json, confidence, created_at)
                VALUES ('route-pr-2886', ?, 'new-pr', 'new_pr', '["push_existing_pr", "open_pr", "comment"]', '["fast-small-pr"]', 'high', ?)
                """,
                (task_id, now),
            )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, worktree_path, branch_name, started_at, heartbeat_at)
                VALUES (?, ?, 1, 'prepared', ?, 'codex/dd-2884-task', ?, ?)
                """,
                (
                    attempt_id,
                    task_id,
                    str(old_worktree_path),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO artifacts(artifact_id, task_id, attempt_id, artifact_type, path, created_at)
                VALUES ('artifact-pr-2886', ?, ?, 'prompt', ?, ?)
                """,
                (task_id, attempt_id, str(prompt_path), now),
            )

        pr_fixture = self.root / "pr-2886-live-followup.json"
        pr_fixture.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "id": "pr-2886-followup",
                            "number": 2886,
                            "source_type": "pull_request",
                            "event_type": "comment",
                            "actor_login": "wklken",
                            "author_association": "COLLABORATOR",
                            "body": "@robert-bot handle the major review items",
                            "intent": "bug_fix",
                            "has_open_dd_pr": True,
                            "existing_pr_head_branch": "codex/issue-2884-fix-operator",
                            "metadata": {
                                "dd_workstream": {
                                    "origin_workstream_id": "github:example/backend#2884",
                                    "source_issue": "2884",
                                }
                            },
                            "workstream_id": "github:example/backend!2886",
                            "origin_workstream_id": "github:example/backend#2884",
                            "event_at": "2026-06-17T09:53:31Z",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        dispatch_calls = []
        prepare_calls = []
        original_dispatch_worker = run_once.dispatch.dispatch_worker
        original_prepare_worktree = run_once._prepare_worktree

        def fake_prepare_worktree(repo, event, route_result, dry_run):
            prepare_calls.append(dry_run)
            self.assertFalse(dry_run)
            return {
                "worktree_path": str(new_worktree_path),
                "branch_name": "codex/issue-2884-fix-operator",
            }

        def fake_dispatch_worker(**kwargs):
            dispatch_calls.append(kwargs)
            return {"ok": True, "status": "running", "pid": 456, "command": ["cbc"]}

        try:
            run_once._prepare_worktree = fake_prepare_worktree
            run_once.dispatch.dispatch_worker = fake_dispatch_worker
            result = run_once.run_once(
                self.config_path,
                workflow_path=PACKAGE_ROOT / "resources" / "workflow.yml",
                fixture_path=pr_fixture,
                dry_run=False,
                skip_external=True,
            )
        finally:
            run_once.dispatch.dispatch_worker = original_dispatch_worker
            run_once._prepare_worktree = original_prepare_worktree

        self.assertTrue(result["ok"], result)
        self.assertEqual(prepare_calls, [False])
        self.assertEqual(len(dispatch_calls), 1)
        self.assertEqual(dispatch_calls[0]["worktree_path"], str(new_worktree_path))
        with closing(sqlite3.connect(db_path)) as conn, conn:
            attempt = conn.execute(
                """
                SELECT worktree_path, branch_name
                FROM attempts
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
        self.assertEqual(attempt, (str(new_worktree_path), "codex/issue-2884-fix-operator"))

    def test_publish_ready_actions_filters_by_repo_id(self):
        from robert_agent import publish
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for repo_id, full_name in [("repo:a", "Org/a"), ("repo:b", "Org/b")]:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, 'robot', 'main', ?, ?)",
                    (repo_id, full_name, str(self.repo_root), str(self.worktree_root)),
                )
                source_id = f"source:{repo_id}"
                workstream_id = f"github:{full_name}#1"
                task_id = f"task:{repo_id}"
                result_id = f"result:{repo_id}"
                conn.execute(
                    "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) VALUES (?, ?, ?, 'issue', 1)",
                    (source_id, repo_id, workstream_id),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                    (workstream_id, repo_id, source_id, now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'completed', ?, ?)",
                    (task_id, workstream_id, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, finished_at) VALUES (?, ?, 1, 'completed', ?, ?)",
                    (f"attempt:{repo_id}", task_id, now, now),
                )
                conn.execute(
                    "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at) VALUES (?, ?, ?, 'comment', ?)",
                    (result_id, task_id, f"attempt:{repo_id}", now),
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, publish_status, created_at, metadata_json) VALUES (?, ?, ?, 'comment', ?, 'accepted', 'not_published', ?, ?)",
                    (
                        f"action:{repo_id}",
                        result_id,
                        task_id,
                        f"https://github.com/{full_name}/issues/1",
                        now,
                        json.dumps({"body": "[from-codex local repo] hi"}, sort_keys=True),
                    ),
                )

        result = publish.publish_ready_actions(db_path, dry_run=True, repo_id="repo:a")

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pending_count"], 1)

    def test_run_once_publish_filters_single_config_repo_when_db_has_other_repo_actions(self):
        from robert_agent import run_once
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for repo_id, full_name in [
                ("repo:example/backend", "example/backend"),
                ("repo:other", "Org/other"),
            ]:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, 'robot', 'main', ?, ?)",
                    (repo_id, full_name, str(self.repo_root), str(self.worktree_root)),
                )
                source_id = f"source:{repo_id}"
                workstream_id = f"github:{full_name}#1"
                task_id = f"task:{repo_id}"
                result_id = f"result:{repo_id}"
                conn.execute(
                    "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) VALUES (?, ?, ?, 'issue', 1)",
                    (source_id, repo_id, workstream_id),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                    (workstream_id, repo_id, source_id, now, now),
                )
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, 'completed', ?, ?)",
                    (task_id, workstream_id, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, finished_at) VALUES (?, ?, 1, 'completed', ?, ?)",
                    (f"attempt:{repo_id}", task_id, now, now),
                )
                conn.execute(
                    "INSERT INTO worker_results(result_id, task_id, attempt_id, output_type, created_at) VALUES (?, ?, ?, 'comment', ?)",
                    (result_id, task_id, f"attempt:{repo_id}", now),
                )
                conn.execute(
                    "INSERT INTO github_actions(action_id, result_id, task_id, action_type, target_url, audit_status, publish_status, created_at, metadata_json) VALUES (?, ?, ?, 'comment', ?, 'accepted', 'not_published', ?, ?)",
                    (
                        f"action:{repo_id}",
                        result_id,
                        task_id,
                        f"https://github.com/{full_name}/issues/1",
                        now,
                        json.dumps({"body": "[from-codex local repo] hi"}, sort_keys=True),
                    ),
                )

        result = run_once._publish_ready_actions_for_repo(
            db_path,
            dry_run=True,
            repo_id="repo:example/backend",
            repo_count=1,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pending_count"], 1)

    def test_running_attempt_count_for_repos_counts_all_configured_repos(self):
        from robert_agent import run_once
        from robert_agent import storage

        db_path = self.data_dir / "dd.sqlite3"
        storage.init_database(db_path)
        now = "2026-07-04T00:00:00+00:00"
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            for repo_id, full_name in [("repo:a", "Org/a"), ("repo:b", "Org/b"), ("repo:c", "Org/c")]:
                conn.execute(
                    "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES (?, ?, 'robot', 'main', ?, ?)",
                    (repo_id, full_name, str(self.repo_root), str(self.worktree_root)),
                )
                source_id = f"source:{repo_id}"
                workstream_id = f"github:{full_name}#1"
                task_id = f"task:{repo_id}"
                attempt_id = f"attempt:{repo_id}"
                conn.execute(
                    "INSERT INTO github_sources(source_id, repo_id, source_key, source_type, number) VALUES (?, ?, ?, 'issue', 1)",
                    (source_id, repo_id, workstream_id),
                )
                conn.execute(
                    "INSERT INTO workstreams(workstream_id, repo_id, primary_source_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                    (workstream_id, repo_id, source_id, now, now),
                )
                lifecycle = "running" if repo_id != "repo:c" else "completed"
                conn.execute(
                    "INSERT INTO tasks(task_id, workstream_id, lifecycle, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, workstream_id, lifecycle, now, now),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id, task_id, attempt_no, status) VALUES (?, ?, 1, 'running')",
                    (attempt_id, task_id),
                )
            count = run_once._running_attempt_count_for_repos(conn, ["repo:a", "repo:b"])

        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()

"""Repository-cycle scenarios share local checkout and attempt builders."""

from contextlib import closing
import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tests.support import init_test_checkout
from robert_agent.adapters.processes import worker as cycle_worker
from robert_agent.execution.application import dispatch as dispatch_queue


def _dd_comment_body(text, **_identity):
    return text


def _new_pr_actions(**_assignment):
    return [
        {"type": "push_existing_pr"},
        {
            "type": "open_pr",
            "title": "Fix issue",
            "body": "Opened PR",
            "issue_resolution": "complete",
        },
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


def _verification_evidence(
    command="python -m unittest", status="passed", required=True
):
    entry = {
        "command": command,
        "status": status,
        "purpose": "Verify the worker result before agent publication.",
        "required": required,
        "source": "worker_reported",
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


class RepositoryCycleFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_root = self.root / "blueking-apigateway"
        self.repo_root.mkdir()
        init_test_checkout(self.repo_root, "master")
        self.worktree_root = self.repo_root / ".worktrees"
        self.worktree_root.mkdir()
        self.data_dir = self.root / "data"
        self.config_path = self.root / "config.yml"
        self.config_path.write_text(
            f"""version: 2
data_dir: {self.data_dir}
database: dd.sqlite3
github:
  account: robert-bot
default_worker: default
workers:
  default:
    adapter: cbc
    command: cbc
    model: gpt-5.4
    effort: high
scheduler:
  max_concurrency: 3
supervision:
  heartbeat_stale_seconds: 1200
  hard_timeout_seconds: 5400
repos:
  - full_name: example/backend
    github_account: robert-bot
    trusted_actors:
    - wklken
    checkout: {self.repo_root}
    worktrees: {self.worktree_root}
    default_branch: master
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

    def _dispatch_model_after_failures(
        self,
        failures,
        fallback_model="model-fallback",
        same_task=False,
    ):
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.data_dir / f"fallback-{len(failures)}.sqlite3"
        storage.init_database(db_path)
        prompt_path = self.root / f"fallback-{len(failures)}.md"
        prompt_path.write_text("retry task", encoding="utf-8")
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id, full_name, github_account, default_base_branch, repo_root, worktree_root) VALUES ('repo-1', 'example/repo', 'robot', 'main', ?, ?)",
                (str(self.repo_root), str(self.worktree_root)),
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id, repo_id, lifecycle, active_task_id, created_at, updated_at) VALUES ('stream-1', 'repo-1', 'active', 'task-current', '2026-10-19T00:00:00+00:00', '2026-10-19T00:03:00+00:00')"
            )
            if same_task:
                conn.execute(
                    """
                    INSERT INTO tasks(
                      task_id, workstream_id, lifecycle, priority, route_id,
                      expected_output, created_at, updated_at
                    ) VALUES ('task-current', 'stream-1', 'queued', 'P1',
                              'new-pr', 'new_pr',
                              '2026-10-19T00:00:00+00:00',
                              '2026-10-19T00:03:00+00:00')
                    """
                )
            for index, failure in enumerate(failures, start=1):
                task_id = "task-current" if same_task else f"task-prior-{index}"
                attempt_id = f"attempt-prior-{index}"
                timestamp = f"2026-10-19T00:0{index}:00+00:00"
                if not same_task:
                    conn.execute(
                        """
                        INSERT INTO tasks(
                          task_id, workstream_id, lifecycle, priority, route_id,
                          expected_output, created_at, updated_at
                        ) VALUES (?, 'stream-1', ?, 'P1', 'new-pr',
                                  'new_pr', ?, ?)
                        """,
                        (
                            task_id,
                            "completed" if failure is None else "failed",
                            timestamp,
                            timestamp,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, finished_at, failure_json, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{"result_contract":"compact-v1"}')
                    """,
                    (
                        attempt_id,
                        task_id,
                        index if same_task else 1,
                        "completed" if failure is None else "failed",
                        timestamp,
                        timestamp,
                        timestamp,
                        (
                            None
                            if failure is None
                            else json.dumps(failure, sort_keys=True)
                        ),
                    ),
                )
            if not same_task:
                conn.execute(
                    """
                    INSERT INTO tasks(
                      task_id, workstream_id, lifecycle, priority, route_id,
                      expected_output, created_at, updated_at
                    ) VALUES ('task-current', 'stream-1', 'queued', 'P1',
                              'new-pr', 'new_pr',
                              '2026-10-19T00:03:00+00:00',
                              '2026-10-19T00:03:00+00:00')
                    """
                )
            conn.execute(
                """
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, metadata_json) VALUES ('attempt-current', 'task-current', ?, 'prepared', '2026-10-19T00:03:00+00:00', '2026-10-19T00:03:00+00:00', '{"result_contract":"compact-v1"}')
                """,
                (len(failures) + 1 if same_task else 1,),
            )

        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE tasks SET metadata_json=? WHERE task_id=?",
                (
                    json.dumps({"trigger_work_item_event_id": "local-request"}),
                    "task-current",
                ),
            )

        worker = {
            "name": "default",
            "agent": "codex",
            "command": "codex",
            "default_model": "model-default",
            "fallback_model": fallback_model,
            "default_effort": "high",
        }
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
                            "task_id": "task-current",
                            "attempt_id": "attempt-current",
                            "workstream_id": "stream-1",
                            "route_id": "new-pr",
                            "prompt_path": prompt_path,
                        },
                        "worktree_result": None,
                        "repo_id": "repo-1",
                        "repo": {"max_concurrency": 1},
                    }
                ],
                True,
                1,
                workers=[worker],
                default_worker=worker,
                config_result={
                    "default_worker": worker,
                    "workers": [worker],
                    "routes": {
                        "new-pr": {
                            "worker": "default",
                            "model": "route-model",
                            "effort": "high",
                        }
                    },
                },
            )
        finally:
            cycle_worker.dispatch_worker = original
        return calls[0]

    @staticmethod
    def _recorded_identity(pid, source="ps"):
        return {
            "pid": pid,
            "process_group_id": pid,
            "process_start_token": f"start-{pid}",
            "command_fingerprint": "a" * 64,
            "identity_source": source,
        }

    def _materialize_worktree(self, path, branch):
        path = Path(path)
        self.assertTrue(path.is_relative_to(self.root), path)
        path.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            for argv in (
                ["init", "-b", branch],
                [
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "--allow-empty",
                    "-m",
                    "fixture",
                ],
            ):
                subprocess.run(
                    ["git", *argv], cwd=path, check=True, capture_output=True
                )

    def _new_pr_actions_for_attempt(
        self,
        db_path,
        task_id,
        issue_number="123",
        pr_number="9",
    ):
        with closing(sqlite3.connect(db_path)) as conn:
            worktree_path, branch = conn.execute(
                """
                SELECT worktree_path, branch_name
                FROM attempts
                WHERE task_id = ?
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        self._materialize_worktree(worktree_path, branch)
        return _new_pr_actions(
            task_id=task_id,
            issue_number=issue_number,
            pr_number=pr_number,
            worktree_path=worktree_path,
            branch=branch,
        )

    def _seed_active_pr_task(self, pr_number):
        from robert_agent.adapters.sqlite import database as storage

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
                INSERT INTO attempts(attempt_id, task_id, attempt_no, status, started_at, heartbeat_at, metadata_json)
                VALUES (?, ?, 1, 'prepared', ?, ?, '{"result_contract":"compact-v1"}')
                """,
                (attempt_id, task_id, now, now),
            )
        return db_path, task_id, attempt_id

    def _interrupted_attempt(self, *, phase=True, transport=False):
        from robert_agent.runtime import cycle as run_once

        first = run_once.run_once(
            self.config_path,
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
            conn.execute("UPDATE tasks SET lifecycle = 'running'")
            conn.execute(
                "UPDATE attempts SET status='running', started_at=?, heartbeat_at=?, metadata_json = json_set(?, '$.result_contract', 'compact-v1')",
                (now, now, json.dumps({"dispatch": self._recorded_identity(987654)})),
            )
            if phase:
                conn.execute(
                    "INSERT INTO worker_phases(phase_id,attempt_id,phase,status,summary,next_step,created_at)"
                    " VALUES ('stale-phase', ?, 'verify', 'running',"
                    " 'Starting command', 'Wait for command completion', ?)",
                    (attempt_id, now),
                )
            if transport:
                log = self.data_dir / "artifacts" / task_id / f"{attempt_id}.stdout.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {
                                "message": "stream disconnected before completion: stream closed before response.completed",
                            },
                        }
                    )
                    + "\n"
                )
                conn.execute(
                    "INSERT INTO artifacts(artifact_id,task_id,attempt_id,artifact_type,path,created_at)"
                    " VALUES ('transport-log',?,?, 'worker_stdout',?,?)",
                    (task_id, attempt_id, str(log), now),
                )
        return db_path, task_id, attempt_id

    def _rate_limited_attempt(
        self, *, fallback="model-fallback", model="model-primary", phase=False
    ):
        db_path, task_id, attempt_id = self._interrupted_attempt(
            phase=phase, transport=True
        )
        log = self.data_dir / "artifacts" / task_id / f"{attempt_id}.stdout.log"
        log.write_text(
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": "exceeded retry limit, last status: 429 Too Many Requests",
                    },
                }
            )
            + "\n"
        )
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "UPDATE attempts SET metadata_json=json_set(metadata_json, '$.dispatch.model', ?,"
                " '$.dispatch.worker_name', 'default', '$.dispatch.fallback_model', ?)",
                (model, fallback),
            )
        return db_path, task_id, attempt_id

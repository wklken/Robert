from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest


class FixtureRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        response = self.responses.get(tuple(args))
        if response is None:
            raise AssertionError(f"unexpected command: {args}")
        stdout = response if isinstance(response, str) else json.dumps(response)
        return subprocess.CompletedProcess(args, 0, stdout, "")


class PrRemediationIntegrationTests(unittest.TestCase):
    def setUp(self):
        from robert_agent import storage

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "robert.sqlite3"
        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                )
                VALUES ('repo:example/backend', 'example/backend', 'robert-bot',
                        'main', '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  title, state, author_login
                )
                VALUES
                  ('source:issue', 'repo:example/backend',
                   'github:example/backend#1', 'issue', 1, 'Origin', 'open',
                   'wklken'),
                  ('source:pr', 'repo:example/backend',
                   'github:example/backend!13', 'pull_request', 13, 'Fix',
                   'open', 'robert-bot')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id, lifecycle,
                  created_at, updated_at
                )
                VALUES ('github:example/backend#1', 'repo:example/backend',
                        'source:issue', 'completed', '2026-07-26', '2026-07-26')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id,
                  origin_workstream_id, lifecycle, created_at, updated_at
                )
                VALUES ('github:example/backend!13', 'repo:example/backend',
                        'source:pr', 'github:example/backend#1', 'completed',
                        '2026-07-26', '2026-07-26')
                """
            )
        self.repo = {
            "full_name": "example/backend",
            "github_account": "robert-bot",
            "trusted_actors": ["wklken"],
            "default_base_branch": "main",
            "repo_root": "/repo",
            "worktree_root": "/worktrees",
            "pr_automation": {
                "conflict": {
                    "enabled": True,
                    "max_attempts": 2,
                    "max_wall_minutes": 60,
                },
                "ci": {
                    "enabled": True,
                    "check_allowlist": ["unit"],
                    "max_attempts": 2,
                    "max_wall_minutes": 120,
                    "max_total_tokens": 10000,
                    "max_cost_usd": 10.0,
                    "max_failure_summary_chars": 1000,
                },
            },
        }

    def runner(self, *, author="robert-bot", mergeable=False):
        return FixtureRunner(
            {
                ("gh", "api", "repos/example/backend/pulls/13"): {
                    "state": "open",
                    "merged": False,
                    "mergeable": mergeable,
                    "head": {
                        "sha": "head-1",
                        "ref": "codex/fix",
                        "repo": {"full_name": "example/backend"},
                    },
                    "base": {"sha": "base-1", "ref": "main"},
                    "user": {"login": author},
                    "html_url": "https://github.com/example/backend/pull/13",
                    "updated_at": "2026-07-26T00:00:00Z",
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs?head_sha=head-1&per_page=100",
                ): {
                    "workflow_runs": [
                        {
                            "id": 101,
                            "run_attempt": 1,
                            "name": "unit",
                            "head_sha": "head-1",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://github.com/run/101",
                        }
                    ]
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs/101/jobs?per_page=100",
                ): {
                    "jobs": [
                        {
                            "id": 501,
                            "name": "python",
                            "conclusion": "failure",
                        }
                    ]
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/jobs/501/logs",
                ): "FAIL tests/test_api.py expected 1 got 2",
                (
                    "gh",
                    "api",
                    "repos/example/backend/commits/head-1/check-runs?per_page=100",
                ): {"check_runs": []},
            }
        )

    def test_collector_creates_system_events_and_conflict_first_decision(self):
        from robert_agent import run_once

        runner = self.runner()
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            decisions = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                runner,
                "2026-07-26T00:01:00Z",
            )
            event_rows = conn.execute(
                """
                SELECT actor_kind, authorization_status, event_type
                FROM github_events
                ORDER BY event_type
                """
            ).fetchall()

        self.assertEqual(
            [decision["task_kind"] for decision in decisions],
            ["merge_conflict_remediation", "ci_remediation"],
        )
        self.assertTrue(
            all(row[0] == "github_system" for row in event_rows),
        )
        self.assertTrue(
            all(row[1] == "authorized_system_trigger" for row in event_rows),
        )
        self.assertIn(
            "pr_merge_conflict_detected",
            [row[2] for row in event_rows],
        )
        self.assertIn("ci_run_completed", [row[2] for row in event_rows])

    def test_collector_is_idempotent_and_ignores_non_robert_owned_pr(self):
        from robert_agent import run_once

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            first = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )
            second = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:02:00Z",
            )
            counts = (
                conn.execute(
                    "SELECT COUNT(*) FROM pr_remediation_episodes"
                ).fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) FROM github_events"
                ).fetchone()[0],
            )

        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(counts, (2, 2))

        other_db = Path(self.tmp.name) / "other.sqlite3"
        self.db_path = other_db
        self.setUp_for_non_owned()
        with closing(sqlite3.connect(other_db)) as conn, conn:
            decisions = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(author="someone-else"),
                "2026-07-26T00:01:00Z",
            )
            episode_count = conn.execute(
                "SELECT COUNT(*) FROM pr_remediation_episodes"
            ).fetchone()[0]
        self.assertEqual(decisions, [])
        self.assertEqual(episode_count, 0)

    def test_pending_system_event_waits_for_fresh_health_reconciliation(self):
        from robert_agent import run_once

        event = {
            "source_key": "github:example/backend!13",
            "source_type": "pull_request",
            "number": 13,
            "event_fingerprint": "github-system:workflow_run:101:1",
            "actor_kind": "github_system",
            "task_kind": "ci_remediation",
            "has_open_dd_pr": True,
            "intent": "pr_followup_fix",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            child = run_once._create_child_task_for_pending_events(
                conn,
                Path(self.tmp.name),
                self.db_path,
                self.repo,
                "repo:example/backend",
                None,
                "github:example/backend!13",
                [event],
                "2026-07-26T00:02:00Z",
                True,
                [],
                {"remaining": 1},
            )
            workstream_state = conn.execute(
                """
                SELECT lifecycle, active_task_id
                FROM workstreams
                WHERE workstream_id = 'github:example/backend!13'
                """
            ).fetchone()
            task_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE workstream_id = 'github:example/backend!13'
                """
            ).fetchone()[0]

        self.assertIsNone(child)
        self.assertEqual(workstream_state, ("completed", None))
        self.assertEqual(task_count, 0)

    def test_pending_system_event_does_not_discard_later_human_event(self):
        from robert_agent import run_once

        system_event = {
            "repo": "example/backend",
            "source_key": "github:example/backend!13",
            "source_type": "pull_request",
            "number": 13,
            "workstream_id": "github:example/backend!13",
            "event_fingerprint": "github-system:workflow_run:101:1",
            "event_type": "ci_run_completed",
            "actor_kind": "github_system",
            "actor_login": "github",
            "authorization_status": "authorized_system_trigger",
            "task_kind": "ci_remediation",
            "has_open_dd_pr": True,
            "intent": "pr_followup_fix",
        }
        human_event = {
            "repo": "example/backend",
            "source_key": "github:example/backend!13",
            "source_type": "pull_request",
            "number": 13,
            "workstream_id": "github:example/backend!13",
            "event_fingerprint": "comment:human-1",
            "event_type": "comment",
            "actor_kind": "github_user",
            "actor_login": "wklken",
            "authorization_status": "authorized_trigger",
            "intent": "analysis",
            "url": "https://github.com/example/backend/pull/13",
        }
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            run_once._insert_source_and_event(
                conn,
                "repo:example/backend",
                system_event,
                "2026-07-26T00:01:00Z",
            )
            run_once._insert_source_and_event(
                conn,
                "repo:example/backend",
                human_event,
                "2026-07-26T00:01:00Z",
            )
            child = run_once._create_child_task_for_pending_events(
                conn,
                Path(self.tmp.name),
                self.db_path,
                self.repo,
                "repo:example/backend",
                None,
                "github:example/backend!13",
                [system_event, human_event],
                "2026-07-26T00:02:00Z",
                True,
                [],
                {"remaining": 1},
            )
            trigger = conn.execute(
                """
                SELECT ge.event_fingerprint
                FROM task_events te
                JOIN github_events ge ON ge.event_id = te.event_id
                WHERE te.task_id = ?
                  AND te.relationship = 'trigger'
                """,
                (child["task_id"],),
            ).fetchone()[0]

        self.assertIsNotNone(child)
        self.assertEqual(trigger, "comment:human-1")

    def test_conflict_decision_creates_exact_snapshot_remediation_task(self):
        from robert_agent import route, run_once, workstream

        runner = self.runner()
        data_dir = Path(self.tmp.name) / "data"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            decisions = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                runner,
                "2026-07-26T00:01:00Z",
            )
            event = decisions[0]
            route_result = route.route_task(event)
            worktree_result = run_once._prepare_worktree(
                self.repo,
                event,
                route_result,
                True,
            )
            task = run_once._create_task_attempt_and_prompt(
                conn,
                data_dir,
                self.db_path,
                self.repo,
                "repo:example/backend",
                event,
                route_result,
                workstream.plan_event(event, active_workstreams=set()),
                "2026-07-26T00:01:00Z",
                worktree_result=worktree_result,
            )
            task_row = conn.execute(
                """
                SELECT task_kind, metadata_json
                FROM tasks
                WHERE task_id = ?
                """,
                (task["task_id"],),
            ).fetchone()
            episode_status = conn.execute(
                """
                SELECT status
                FROM pr_remediation_episodes
                WHERE episode_id = ?
                """,
                (
                    json.loads(task_row[1])[
                        "remediation_episode_id"
                    ],
                ),
            ).fetchone()[0]

        prompt = Path(task["prompt_path"]).read_text(encoding="utf-8")
        self.assertEqual(task_row[0], "merge_conflict_remediation")
        self.assertEqual(episode_status, "remediating")
        self.assertEqual(worktree_result["observed_head_sha"], "head-1")
        self.assertEqual(worktree_result["observed_base_sha"], "base-1")
        self.assertIn(
            "git merge --no-edit refs/robert/prs/pr-13-base",
            prompt,
        )

    def test_dirty_remediation_worktree_exhausts_episode_without_task(self):
        from robert_agent import run_once

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            decisions = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )
            event = decisions[0]
            blocked = run_once._handle_blocked_remediation_worktree(
                conn,
                event,
                {
                    "ok": False,
                    "status": "blocked_dirty_worktree",
                    "worktree_path": "/worktrees/pr-13-write",
                },
                "2026-07-26T00:02:00Z",
            )
            episode = conn.execute(
                """
                SELECT status, metadata_json
                FROM pr_remediation_episodes
                WHERE episode_id = ?
                """,
                (
                    event["metadata"]["remediation"][
                        "episode_id"
                    ],
                ),
            ).fetchone()
            task_count = conn.execute(
                "SELECT COUNT(*) FROM tasks"
            ).fetchone()[0]

        self.assertTrue(blocked)
        self.assertEqual(episode[0], "exhausted")
        self.assertEqual(
            json.loads(episode[1])["last_transition_reason"],
            "blocked_dirty_worktree",
        )
        self.assertEqual(task_count, 0)

    def test_failed_system_task_can_retry_same_guarded_event(self):
        from robert_agent import route, run_once, workstream

        data_dir = Path(self.tmp.name) / "retry-data"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            event = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:01:00Z",
            )[0]
            route_result = route.route_task(event)
            task = run_once._create_task_attempt_and_prompt(
                conn,
                data_dir,
                self.db_path,
                self.repo,
                "repo:example/backend",
                event,
                route_result,
                workstream.plan_event(event, active_workstreams=set()),
                "2026-07-26T00:01:00Z",
                worktree_result=run_once._prepare_worktree(
                    self.repo,
                    event,
                    route_result,
                    True,
                ),
            )
            run_once._finalize_failed_task(
                conn,
                task["task_id"],
                task["workstream_id"],
                "2026-07-26T00:02:00Z",
                {"status": "worker_failed"},
            )
            retry_events = run_once._collect_pr_remediation_decisions(
                conn,
                "repo:example/backend",
                self.repo,
                self.runner(),
                "2026-07-26T00:03:00Z",
            )

        self.assertEqual(
            retry_events[0]["event_fingerprint"],
            event["event_fingerprint"],
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertFalse(
                run_once._should_skip_terminal_trigger(
                    conn,
                    retry_events[0],
                )
            )

    def setUp_for_non_owned(self):
        from robert_agent import storage

        storage.init_database(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO repos(
                  repo_id, full_name, github_account, default_base_branch,
                  repo_root, worktree_root
                )
                VALUES ('repo:example/backend', 'example/backend', 'robert-bot',
                        'main', '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO github_sources(
                  source_id, repo_id, source_key, source_type, number,
                  title, state
                )
                VALUES ('source:pr', 'repo:example/backend',
                        'github:example/backend!13', 'pull_request', 13,
                        'Fix', 'open')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id,
                  origin_workstream_id, lifecycle, created_at, updated_at
                )
                VALUES ('origin', 'repo:example/backend', NULL, NULL,
                        'completed', '2026-07-26', '2026-07-26')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, primary_source_id,
                  origin_workstream_id, lifecycle, created_at, updated_at
                )
                VALUES ('github:example/backend!13', 'repo:example/backend',
                        'source:pr', 'origin', 'completed',
                        '2026-07-26', '2026-07-26')
                """
            )


if __name__ == "__main__":
    unittest.main()

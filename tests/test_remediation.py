from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


class RemediationTests(unittest.TestCase):
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
                VALUES ('repo-1', 'example/backend', 'robert-bot', 'main',
                        '/repo', '/worktrees')
                """
            )
            conn.execute(
                """
                INSERT INTO workstreams(
                  workstream_id, repo_id, origin_workstream_id, lifecycle,
                  created_at, updated_at
                )
                VALUES ('pr-ws', 'repo-1', NULL, 'completed',
                        '2026-07-26T00:00:00Z', '2026-07-26T00:00:00Z')
                """
            )
        self.repo = {
            "repo_id": "repo-1",
            "pr_automation": {
                "conflict": {
                    "enabled": True,
                    "max_attempts": 2,
                    "max_wall_minutes": 60,
                },
                "ci": {
                    "enabled": True,
                    "check_allowlist": ["unit", "lint"],
                    "max_attempts": 2,
                    "max_wall_minutes": 120,
                    "max_total_tokens": 1000,
                    "max_cost_usd": 10.0,
                },
            },
        }
        self.workstream = {
            "workstream_id": "pr-ws",
            "repo_id": "repo-1",
            "active_task_id": None,
            "number": 13,
        }
        self.snapshot = {
            "state": "open",
            "merged": False,
            "mergeable": False,
            "head_sha": "head-1",
            "base_sha": "base-1",
        }

    def connect(self):
        return sqlite3.connect(self.db_path)

    def episodes(self):
        with closing(self.connect()) as conn:
            return conn.execute(
                """
                SELECT episode_kind, subject_key, status, attempt_count,
                       failure_signature, result_head_sha
                FROM pr_remediation_episodes
                ORDER BY episode_kind, first_seen_at
                """
            ).fetchall()

    def test_upsert_is_idempotent_and_prioritizes_conflict_over_ci(self):
        from robert_agent import remediation

        ci_summary = {
            "status": "failing",
            "failure_signature": "failure-1",
            "failures": [
                {
                    "source_kind": "workflow_run",
                    "external_id": "101",
                    "attempt_no": 1,
                    "check_name": "unit",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ],
            "observations": [],
        }
        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary=ci_summary,
                now="2026-07-26T00:01:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary=ci_summary,
                now="2026-07-26T00:02:00Z",
            )
            decisions = remediation.next_system_decisions(
                conn,
                repo=self.repo,
                now="2026-07-26T00:02:00Z",
            )

        self.assertEqual(
            self.episodes(),
            [
                (
                    "ci",
                    "head-1:base-1",
                    "open",
                    0,
                    "failure-1",
                    None,
                ),
                (
                    "merge_conflict",
                    "head-1:base-1",
                    "open",
                    0,
                    None,
                    None,
                ),
            ],
        )
        self.assertEqual(
            [decision["episode_kind"] for decision in decisions],
            ["merge_conflict", "ci"],
        )

    def test_new_snapshot_supersedes_nonterminal_old_episode(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={
                    **self.snapshot,
                    "head_sha": "head-2",
                    "mergeable": False,
                },
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:02:00Z",
            )

        self.assertEqual(
            [row[2] for row in self.episodes()],
            ["superseded", "open"],
        )

    def test_terminal_pr_cancels_open_episodes(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "state": "closed"},
                ci_summary=None,
                now="2026-07-26T00:02:00Z",
            )

        self.assertEqual(self.episodes()[0][2], "canceled")

    def test_waiting_conflict_resolves_when_result_head_is_mergeable(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            remediation.record_publish_result(
                conn,
                episode_id=episode_id,
                publish_status="published",
                result_head_sha="head-2",
                now="2026-07-26T00:02:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={
                    **self.snapshot,
                    "head_sha": "head-2",
                    "mergeable": True,
                },
                ci_summary={"status": "waiting", "observations": []},
                now="2026-07-26T00:03:00Z",
            )

        self.assertEqual(self.episodes()[0][2], "resolved")
        self.assertEqual(self.episodes()[0][5], "head-2")

    def test_waiting_conflict_exhausts_when_result_head_is_still_conflicted(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            remediation.record_publish_result(
                conn,
                episode_id=episode_id,
                publish_status="published",
                result_head_sha="head-2",
                now="2026-07-26T00:02:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={
                    **self.snapshot,
                    "head_sha": "head-2",
                    "mergeable": False,
                },
                ci_summary={"status": "waiting", "observations": []},
                now="2026-07-26T00:03:00Z",
            )

        self.assertEqual(len(self.episodes()), 1)
        self.assertEqual(self.episodes()[0][2], "exhausted")
        self.assertEqual(self.episodes()[0][5], "head-2")

    def test_waiting_ci_resolves_green_and_exhausts_repeated_signature(self):
        from robert_agent import remediation

        failing = {
            "status": "failing",
            "failure_signature": "same",
            "failures": [],
            "observations": [],
        }
        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "mergeable": True},
                ci_summary=failing,
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            remediation.record_publish_result(
                conn,
                episode_id=episode_id,
                publish_status="published",
                result_head_sha="head-2",
                now="2026-07-26T00:02:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={
                    **self.snapshot,
                    "head_sha": "head-2",
                    "mergeable": True,
                },
                ci_summary=failing,
                now="2026-07-26T00:03:00Z",
            )
        self.assertEqual(len(self.episodes()), 1)
        self.assertEqual(self.episodes()[0][2], "exhausted")

        with closing(self.connect()) as conn, conn:
            conn.execute("DELETE FROM pr_remediation_episodes")
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "mergeable": True},
                ci_summary=failing,
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            remediation.record_publish_result(
                conn,
                episode_id=episode_id,
                publish_status="published",
                result_head_sha="head-2",
                now="2026-07-26T00:02:00Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={
                    **self.snapshot,
                    "head_sha": "head-2",
                    "mergeable": True,
                },
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:03:00Z",
            )
        self.assertEqual(self.episodes()[0][2], "resolved")

    def test_record_task_started_and_publish_result_are_guarded(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle,
                  created_at, updated_at
                )
                VALUES ('task-1', 'pr-ws', 'merge_conflict_remediation',
                        'completed', '2026-07-26T00:01:00Z',
                        '2026-07-26T00:01:00Z')
                """
            )
            self.assertTrue(
                remediation.record_task_started(
                    conn,
                    episode_id=episode_id,
                    task_id="task-1",
                    now="2026-07-26T00:02:00Z",
                )
            )
            self.assertFalse(
                remediation.record_task_started(
                    conn,
                    episode_id=episode_id,
                    task_id="task-1",
                    now="2026-07-26T00:03:00Z",
                )
            )
            remediation.record_publish_result(
                conn,
                episode_id=episode_id,
                publish_status="failed",
                now="2026-07-26T00:04:00Z",
            )

        row = self.episodes()[0]
        self.assertEqual(row[2], "open")
        self.assertEqual(row[3], 1)

    def test_budget_status_enforces_attempt_wall_token_and_cost_limits(self):
        from robert_agent import remediation

        policy = {
            "max_attempts": 2,
            "max_wall_minutes": 60,
            "max_total_tokens": 100,
            "max_cost_usd": 2.0,
        }
        episode = {
            "episode_id": "episode-1",
            "attempt_count": 2,
            "first_seen_at": "2026-07-26T00:00:00Z",
        }
        with closing(self.connect()) as conn:
            self.assertEqual(
                remediation.budget_status(
                    conn,
                    episode=episode,
                    policy=policy,
                    now="2026-07-26T00:01:00Z",
                )["reason"],
                "max_attempts",
            )
            self.assertEqual(
                remediation.budget_status(
                    conn,
                    episode={**episode, "attempt_count": 0},
                    policy=policy,
                    now="2026-07-26T02:00:00Z",
                )["reason"],
                "max_wall_minutes",
            )

            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle,
                  created_at, updated_at, metadata_json
                )
                VALUES ('task-usage', 'pr-ws', 'ci_remediation', 'completed',
                        '2026-07-26T00:00:00Z', '2026-07-26T00:00:00Z',
                        ?)
                """,
                (json.dumps({"remediation_episode_id": "episode-1"}),),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, metadata_json
                )
                VALUES ('attempt-usage', 'task-usage', 1, 'completed', ?)
                """,
                (
                    json.dumps(
                        {
                            "usage": {
                                "usage_available": True,
                                "usage": {
                                    "input_tokens": 70,
                                    "output_tokens": 40,
                                },
                                "total_cost_usd": 2.5,
                            }
                        }
                    ),
                ),
            )
            usage_limited = remediation.budget_status(
                conn,
                episode={**episode, "attempt_count": 0},
                policy={**policy, "max_wall_minutes": 600},
                now="2026-07-26T00:01:00Z",
            )

        self.assertEqual(usage_limited["reason"], "max_total_tokens")
        self.assertEqual(usage_limited["total_tokens"], 110)
        self.assertEqual(usage_limited["total_cost_usd"], 2.5)

    def test_budget_status_stops_when_configured_usage_cannot_be_measured(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle,
                  created_at, updated_at, metadata_json
                )
                VALUES ('task-no-usage', 'pr-ws', 'ci_remediation', 'failed',
                        '2026-07-26T00:00:00Z', '2026-07-26T00:00:00Z',
                        ?)
                """,
                (json.dumps({"remediation_episode_id": "episode-no-usage"}),),
            )
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, task_id, attempt_no, status, metadata_json
                )
                VALUES ('attempt-no-usage', 'task-no-usage', 1, 'failed', '{}')
                """
            )
            budget = remediation.budget_status(
                conn,
                episode={
                    "episode_id": "episode-no-usage",
                    "attempt_count": 1,
                    "first_seen_at": "2026-07-26T00:00:00Z",
                },
                policy={
                    "max_attempts": 2,
                    "max_wall_minutes": 60,
                    "max_total_tokens": 100,
                    "max_cost_usd": 2.0,
                },
                now="2026-07-26T00:01:00Z",
            )

        self.assertEqual(budget["reason"], "usage_unavailable")
        self.assertEqual(budget["measured_attempt_count"], 0)

    def test_attestation_requires_head_base_branch_and_clean_merge_graph(self):
        from robert_agent import remediation

        class Runner:
            def __init__(
                self,
                base_is_ancestor=True,
                current_head="repair-1",
            ):
                self.base_is_ancestor = base_is_ancestor
                self.current_head = current_head

            def __call__(self, args, **_kwargs):
                if args == ["git", "branch", "--show-current"]:
                    return type("Completed", (), {"returncode": 0, "stdout": "codex/fix\n"})()
                if args == ["git", "rev-parse", "HEAD"]:
                    return type(
                        "Completed",
                        (),
                        {
                            "returncode": 0,
                            "stdout": f"{self.current_head}\n",
                        },
                    )()
                if args == ["git", "status", "--porcelain"]:
                    return type(
                        "Completed",
                        (),
                        {"returncode": 0, "stdout": ""},
                    )()
                if args == [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    "head-1",
                    "HEAD",
                ]:
                    return type("Completed", (), {"returncode": 0, "stdout": ""})()
                if args == [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    "base-1",
                    "HEAD",
                ]:
                    return type(
                        "Completed",
                        (),
                        {
                            "returncode": 0 if self.base_is_ancestor else 1,
                            "stdout": "",
                        },
                    )()
                if args == ["git", "diff", "--name-only", "--diff-filter=U"]:
                    return type("Completed", (), {"returncode": 0, "stdout": ""})()
                raise AssertionError(args)

        evidence = {
            "kind": "merge_conflict",
            "episode_id": "episode-conflict",
            "observed_head_sha": "head-1",
            "observed_base_sha": "base-1",
            "resolution_summary": "Resolved value.txt using the new base behavior.",
        }
        scope = {
            "worktree_path": "/tmp/worktree",
            "branch_name": "codex/fix",
        }

        accepted = remediation.attest_remediation_worktree(
            task_kind="merge_conflict_remediation",
            action_scope=scope,
            remediation_evidence=evidence,
            run_command=Runner(),
        )
        rejected = remediation.attest_remediation_worktree(
            task_kind="merge_conflict_remediation",
            action_scope=scope,
            remediation_evidence=evidence,
            run_command=Runner(base_is_ancestor=False),
        )
        unchanged = remediation.attest_remediation_worktree(
            task_kind="merge_conflict_remediation",
            action_scope=scope,
            remediation_evidence=evidence,
            run_command=Runner(current_head="head-1"),
        )

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["reason"], "base_not_ancestor")
        self.assertEqual(unchanged["reason"], "unchanged_head")

    def test_manual_ci_signal_exhausts_episode_and_notifies_operator(self):
        from robert_agent import remediation

        manual = {
            "status": "manual",
            "reason": "failure_evidence_unavailable",
            "failures": [
                {
                    "source_kind": "check_run",
                    "external_id": "501",
                    "attempt_no": 1,
                    "check_name": "unit",
                    "status": "completed",
                    "conclusion": "failure",
                    "evidence_status": "unavailable",
                }
            ],
            "observations": [],
        }
        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "mergeable": True},
                ci_summary=manual,
                now="2026-07-26T00:01:00Z",
            )
            episode = conn.execute(
                """
                SELECT status, metadata_json
                FROM pr_remediation_episodes
                WHERE episode_kind = 'ci'
                """
            ).fetchone()
            notification = conn.execute(
                """
                SELECT notification_type, metadata_json
                FROM notifications
                """
            ).fetchone()

        self.assertEqual(episode[0], "exhausted")
        self.assertEqual(
            json.loads(episode[1])["last_transition_reason"],
            "failure_evidence_unavailable",
        )
        self.assertEqual(notification[0], "pr_remediation_needs_evidence")
        self.assertEqual(
            json.loads(notification[1])["reason"],
            "failure_evidence_unavailable",
        )

    def test_budget_exhaustion_notifies_operator_once(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE pr_remediation_episodes
                SET attempt_count = 2
                WHERE episode_id = ?
                """,
                (episode_id,),
            )
            repo = {**self.repo, "repo_id": "repo-1"}
            self.assertEqual(
                remediation.next_system_decisions(
                    conn,
                    repo=repo,
                    now="2026-07-26T00:02:00Z",
                ),
                [],
            )
            remediation.next_system_decisions(
                conn,
                repo=repo,
                now="2026-07-26T00:03:00Z",
            )
            notifications = conn.execute(
                """
                SELECT COUNT(*)
                FROM notifications
                WHERE notification_type = 'pr_remediation_exhausted'
                """
            ).fetchone()[0]

        self.assertEqual(notifications, 1)

    def test_terminal_pr_cancels_all_nonterminal_episodes(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={
                    "status": "failing",
                    "failure_signature": "failure-1",
                    "failures": [],
                    "observations": [],
                },
                now="2026-07-26T00:01:00Z",
            )
            canceled = remediation.cancel_workstream_episodes(
                conn,
                workstream_id="pr-ws",
                reason="remote_pr_terminal",
                now="2026-07-26T00:02:00Z",
            )
            statuses = conn.execute(
                """
                SELECT episode_kind, status
                FROM pr_remediation_episodes
                ORDER BY episode_kind
                """
            ).fetchall()

        self.assertEqual(canceled, 2)
        self.assertEqual(
            statuses,
            [("ci", "canceled"), ("merge_conflict", "canceled")],
        )

    def test_disabled_policy_cancels_existing_episode_before_decision(self):
        from robert_agent import remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "mergeable": True},
                ci_summary={
                    "status": "failing",
                    "failure_signature": "failure-1",
                    "failures": [],
                    "observations": [],
                },
                now="2026-07-26T00:01:00Z",
            )
            repo = {
                **self.repo,
                "repo_id": "repo-1",
                "pr_automation": {
                    **self.repo["pr_automation"],
                    "ci": {
                        **self.repo["pr_automation"]["ci"],
                        "enabled": False,
                    },
                },
            }
            remediation.cancel_disabled_episodes(
                conn,
                repo=repo,
                now="2026-07-26T00:02:00Z",
            )
            decisions = remediation.next_system_decisions(
                conn,
                repo=repo,
                now="2026-07-26T00:02:00Z",
            )
            status = conn.execute(
                """
                SELECT status
                FROM pr_remediation_episodes
                WHERE episode_kind = 'ci'
                """
            ).fetchone()[0]

        self.assertEqual(decisions, [])
        self.assertEqual(status, "canceled")

    def test_running_episode_keeps_original_failure_evidence(self):
        from robert_agent import remediation

        first = {
            "status": "failing",
            "failure_signature": "failure-1",
            "failures": [{"check_name": "unit", "failure_summary": "one"}],
            "observations": [],
        }
        changed = {
            "status": "failing",
            "failure_signature": "failure-2",
            "failures": [{"check_name": "unit", "failure_summary": "two"}],
            "observations": [],
        }
        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "mergeable": True},
                ci_summary=first,
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute(
                "SELECT episode_id FROM pr_remediation_episodes"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, workstream_id, task_kind, lifecycle,
                  created_at, updated_at
                )
                VALUES ('task-freeze', 'pr-ws', 'ci_remediation',
                        'running', '2026-07-26T00:01:00Z',
                        '2026-07-26T00:01:00Z')
                """
            )
            remediation.record_task_started(
                conn,
                episode_id=episode_id,
                task_id="task-freeze",
                now="2026-07-26T00:01:30Z",
            )
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot={**self.snapshot, "mergeable": True},
                ci_summary=changed,
                now="2026-07-26T00:02:00Z",
            )
            row = conn.execute(
                """
                SELECT failure_signature, metadata_json
                FROM pr_remediation_episodes
                WHERE episode_id = ?
                """,
                (episode_id,),
            ).fetchone()

        self.assertEqual(row[0], "failure-1")
        self.assertEqual(
            json.loads(row[1])["failures"][0]["failure_summary"],
            "one",
        )


if __name__ == "__main__":
    unittest.main()

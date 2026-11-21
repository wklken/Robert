from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


class RemediationTests(unittest.TestCase):
    def setUp(self):
        from robert_agent.persistence.schema import database as storage

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

    def manual_ci(self, *, run_id="501", attempt_no=1, reason="failure_evidence_unavailable"):
        return {
            "status": "manual",
            "reason": reason,
            "failures": [
                {
                    "source_kind": "workflow_run",
                    "external_id": run_id,
                    "attempt_no": attempt_no,
                    "check_name": "unit",
                    "conclusion": "failure",
                    "evidence_status": "unavailable",
                }
            ],
        }

    def observe_ci(self, conn, base, summary=None, head="head-1"):
        from robert_agent.workflows import pr_remediation

        return pr_remediation.upsert_health_episodes(
            conn,
            repo=self.repo,
            workstream=self.workstream,
            snapshot={**self.snapshot, "mergeable": True, "base_sha": base, "head_sha": head},
            ci_summary=summary or self.manual_ci(),
            now="2026-11-20T03:16:48Z",
        )

    def test_ten_base_changes_group_ci_notice_and_preserve_delivery_and_ack(self):
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-0")
            identifier, raw = conn.execute("SELECT notification_id, metadata_json FROM notifications").fetchone()
            original = json.loads(raw)
            original.update(
                delivery={"status": "sent", "delivery_id": "receipt"},
                operator_response={"action": "ack", "request_key": "ack-1"},
            )
            conn.execute("UPDATE notifications SET metadata_json=?", (json.dumps(original),))
            for index in range(1, 10):
                self.observe_ci(conn, f"base-{index}")
            rows = conn.execute("SELECT notification_id, metadata_json FROM notifications").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], identifier)
            grouped = json.loads(rows[0][1])
            self.assertEqual(grouped["observed_base_sha"], "base-9")
            self.assertEqual(grouped["delivery"], original["delivery"])
            self.assertEqual(grouped["operator_response"], original["operator_response"])
            self.assertEqual(conn.execute("SELECT count(*) FROM pr_remediation_episodes").fetchone()[0], 10)

    def test_changed_run_rerun_or_reason_notifies_again(self):
        with closing(self.connect()) as conn, conn:
            for index, summary in enumerate(
                [
                    self.manual_ci(),
                    self.manual_ci(run_id="502"),
                    self.manual_ci(run_id="502", attempt_no=2),
                    self.manual_ci(run_id="502", attempt_no=2, reason="manual_conclusion"),
                ]
            ):
                self.observe_ci(conn, f"base-{index}", summary)
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 4)

    def test_recovered_ci_failure_can_notify_again_on_same_snapshot(self):
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-1")
            self.observe_ci(conn, "base-1", {"status": "green"})
            self.assertEqual(conn.execute("SELECT status FROM notifications").fetchone()[0], "resolved")
            self.observe_ci(conn, "base-1")
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 2)

    def test_canceled_workstream_cannot_create_health_episode_or_notice(self):
        with closing(self.connect()) as conn, conn:
            conn.execute("UPDATE workstreams SET lifecycle='canceled'")
            self.observe_ci(conn, "base-1")
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM pr_remediation_episodes").fetchone()[0], 0)

    def test_new_head_resolves_old_ci_alert(self):
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-1")
            self.observe_ci(conn, "base-1", head="head-2")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM notifications WHERE status='resolved'").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM notifications WHERE status='recorded'").fetchone()[0], 1
            )

    def test_cancelled_derived_pr_is_not_selected_for_scan(self):
        from robert_agent.persistence.queries import workstreams

        with closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('origin','repo-1','now','now')"
            )
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,state) VALUES ('pr-source','repo-1','pr-key','pull_request',13,'open')"
            )
            conn.execute(
                "UPDATE workstreams SET origin_workstream_id='origin', primary_source_id='pr-source' WHERE workstream_id='pr-ws'"
            )
            self.assertEqual(
                [r[0] for r in workstreams.find_for_collect_pr_remediation_decisions(conn, "repo-1")], ["pr-ws"]
            )
            conn.execute("UPDATE workstreams SET lifecycle='canceled' WHERE workstream_id='pr-ws'")
            self.assertEqual(workstreams.find_for_collect_pr_remediation_decisions(conn, "repo-1"), [])

    def test_same_snapshot_new_ci_run_notifies_again(self):
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-1")
            self.observe_ci(conn, "base-1", self.manual_ci(run_id="502"))
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 2)

    def test_manual_conclusion_groups_by_run_without_failure_details(self):
        with closing(self.connect()) as conn, conn:
            for index, run_id in enumerate(["501", "501", "502"]):
                summary = self.manual_ci(run_id=run_id, reason="manual_conclusion")
                summary["observations"] = summary.pop("failures")
                summary["observations"][0]["conclusion"] = "action_required"
                summary["manual_conclusions"] = ["action_required"]
                self.observe_ci(conn, f"base-{index}", summary)
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 2)

    def test_base_changes_send_ci_notice_only_once_after_restart(self):
        from robert_agent.delivery.application import notifications
        from types import SimpleNamespace
        from unittest.mock import Mock

        runner = Mock(return_value=SimpleNamespace(returncode=0))
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-0")
        self.assertEqual(notifications.deliver_pending(self.db_path, command=["notify"], runner=runner)["sent"], 1)
        for index in range(1, 10):
            with closing(self.connect()) as conn, conn:
                self.observe_ci(conn, f"base-{index}")
            self.assertEqual(notifications.deliver_pending(self.db_path, command=["notify"], runner=runner)["sent"], 0)
        self.assertEqual(runner.call_count, 1)

    def test_legacy_notice_is_reused_without_resetting_failed_delivery(self):
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-1")
            identifier, raw = conn.execute("SELECT notification_id,metadata_json FROM notifications").fetchone()
            metadata = json.loads(raw)
            metadata.pop("notification_key")
            metadata["delivery"] = {"status": "failed", "attempt_count": 2, "next_retry_at": "2026-11-20T03:17:00Z"}
            conn.execute("UPDATE notifications SET metadata_json=?", (json.dumps(metadata),))
            self.observe_ci(conn, "base-2")
            rows = conn.execute("SELECT notification_id,metadata_json FROM notifications").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], identifier)
            self.assertEqual(json.loads(rows[0][1])["delivery"], metadata["delivery"])

    def test_existing_duplicate_ci_notices_converge_before_delivery(self):
        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-1")
            raw = conn.execute("SELECT metadata_json FROM notifications").fetchone()[0]
            conn.execute(
                "INSERT INTO notifications(notification_id,notification_type,channel,status,created_at,metadata_json) VALUES ('duplicate','pr_remediation_needs_evidence','local','recorded','2026-11-20T03:17:00Z',?)",
                (raw,),
            )
            self.observe_ci(conn, "base-2")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM notifications WHERE status!='resolved'").fetchone()[0], 1
            )

    def test_cancel_blocks_existing_open_episode_decisions(self):
        from robert_agent.workflows import pr_remediation

        with closing(self.connect()) as conn, conn:
            pr_remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green"},
                now="2026-11-20T03:12:22Z",
            )
            conn.execute("UPDATE workstreams SET lifecycle='canceled'")
            self.assertEqual(pr_remediation.next_system_decisions(conn, repo=self.repo, now="2026-11-20T03:16:48Z"), [])
            self.assertEqual(conn.execute("SELECT count(*) FROM notifications").fetchone()[0], 0)

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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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

    def test_exhausted_result_is_not_retried_on_later_identical_polls(self):
        from robert_agent.workflows import pr_remediation as remediation

        for kind in ("merge_conflict", "ci"):
            with self.subTest(kind=kind), closing(self.connect()) as conn, conn:
                conn.execute("DELETE FROM pr_remediation_episodes")
                snapshot = {**self.snapshot, "mergeable": kind == "ci"}
                summary = {
                    "status": "failing" if kind == "ci" else "waiting",
                    "failure_signature": "same",
                    "failures": [],
                    "observations": [],
                }
                remediation.upsert_health_episodes(
                    conn,
                    repo=self.repo,
                    workstream=self.workstream,
                    snapshot=snapshot,
                    ci_summary=summary,
                    now="2026-07-26T00:01:00Z",
                )
                episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
                remediation.record_publish_result(
                    conn,
                    episode_id=episode_id,
                    publish_status="published",
                    result_head_sha="head-2",
                    now="2026-07-26T00:02:00Z",
                )
                for minute in (3, 4, 5):
                    remediation.upsert_health_episodes(
                        conn,
                        repo=self.repo,
                        workstream=self.workstream,
                        snapshot={**snapshot, "head_sha": "head-2"},
                        ci_summary=summary,
                        now=f"2026-07-26T00:0{minute}:00Z",
                    )
                self.assertEqual(
                    remediation.next_system_decisions(conn, repo=self.repo, now="2026-07-26T00:05:00Z"), []
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pr_remediation_episodes").fetchone()[0], 1)
                remediation.upsert_health_episodes(
                    conn,
                    repo=self.repo,
                    workstream=self.workstream,
                    snapshot={**snapshot, "head_sha": "human-head"},
                    ci_summary=summary,
                    now="2026-07-26T00:06:00Z",
                )
                self.assertEqual(
                    len(remediation.next_system_decisions(conn, repo=self.repo, now="2026-07-26T00:06:00Z")), 1
                )

    def test_waiting_conflict_exhausts_when_result_head_is_still_conflicted(self):
        from robert_agent.workflows import pr_remediation as remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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
        from robert_agent.workflows import pr_remediation as remediation

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
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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
        from robert_agent.workflows import pr_remediation as remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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
        from robert_agent.workflows import pr_remediation as remediation

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

    def test_ci_budget_starts_with_available_evidence_and_survives_evidence_changes(self):
        from robert_agent.workflows import pr_remediation as remediation

        self.repo["pr_automation"]["dispatch_mode"] = "direct"
        snapshot = {**self.snapshot, "mergeable": True}
        available = {"status": "failing", "failure_signature": "failure-1", "failures": []}
        with closing(self.connect()) as conn, conn:

            def observe(summary, now):
                remediation.upsert_health_episodes(
                    conn,
                    repo=self.repo,
                    workstream=self.workstream,
                    snapshot=snapshot,
                    ci_summary=summary,
                    now=now,
                )

            observe(self.manual_ci(), "2026-07-26T00:00:00Z")
            observe(available, "2026-07-26T03:00:00Z")
            first = remediation.next_system_decisions(conn, repo=self.repo, now="2026-07-26T03:00:00Z")
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["first_seen_at"], "2026-07-26T00:00:00Z")
            self.assertEqual(first[0]["budget"]["elapsed_minutes"], 0)
            observe(available, "2026-07-26T03:10:00Z")
            observe(self.manual_ci(), "2026-07-26T03:30:00Z")
            observe(available, "2026-07-26T04:00:00Z")
            repeated = remediation.next_system_decisions(conn, repo=self.repo, now="2026-07-26T04:00:00Z")
            self.assertEqual(repeated[0]["episode_id"], first[0]["episode_id"])
            self.assertEqual(repeated[0]["budget"]["elapsed_minutes"], 60)
            self.assertEqual(repeated[0]["attempt_count"], 0)
            self.assertEqual(remediation.next_system_decisions(conn, repo=self.repo, now="2026-07-26T05:01:00Z"), [])
            final = remediation.load_episode(conn, first[0]["episode_id"])
            self.assertEqual(final["metadata"]["last_transition_reason"], "max_wall_minutes")
            snapshot = {**snapshot, "head_sha": "head-2"}
            observe(available, "2026-07-26T06:00:00Z")
            fresh = remediation.next_system_decisions(conn, repo=self.repo, now="2026-07-26T06:00:00Z")
            self.assertEqual(len(fresh), 1)
            self.assertNotEqual(fresh[0]["episode_id"], first[0]["episode_id"])
            self.assertEqual(fresh[0]["budget"]["elapsed_minutes"], 0)

    def test_legacy_ci_budget_preserves_existing_eligibility_and_attempts(self):
        from robert_agent.workflows import pr_remediation as remediation

        with closing(self.connect()) as conn, conn:
            self.observe_ci(conn, "base-1", {"status": "failing", "failure_signature": "failure-1"})
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
            for attempts, old_status in ((0, "failing"), (1, "manual")):
                with self.subTest(attempts=attempts, old_status=old_status):
                    conn.execute(
                        "UPDATE pr_remediation_episodes SET attempt_count=?, metadata_json=?",
                        (attempts, json.dumps({"ci_status": old_status})),
                    )
                    remediation.upsert_health_episodes(
                        conn,
                        repo=self.repo,
                        workstream=self.workstream,
                        snapshot={**self.snapshot, "mergeable": True},
                        ci_summary={"status": "failing", "failure_signature": "failure-1"},
                        now="2026-11-20T06:16:48Z",
                    )
                    episode = remediation.load_episode(conn, episode_id)
                    self.assertEqual(episode["attempt_count"], attempts)
                    self.assertEqual(
                        remediation.budget_status(
                            conn,
                            episode=episode,
                            policy=self.repo["pr_automation"]["ci"],
                            now="2026-11-20T06:16:48Z",
                        )["reason"],
                        "max_wall_minutes",
                    )

    def test_budget_status_stops_when_configured_usage_cannot_be_measured(self):
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

        with closing(self.connect()) as conn, conn:
            remediation.upsert_health_episodes(
                conn,
                repo=self.repo,
                workstream=self.workstream,
                snapshot=self.snapshot,
                ci_summary={"status": "green", "observations": []},
                now="2026-07-26T00:01:00Z",
            )
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

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
        from robert_agent.workflows import pr_remediation as remediation

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
            episode_id = conn.execute("SELECT episode_id FROM pr_remediation_episodes").fetchone()[0]
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

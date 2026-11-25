"""Independent proposal scheduling remains responsive and creates one scoped daily digest."""

from contextlib import closing
import json
import sqlite3
from unittest.mock import Mock, patch

from robert_agent.runtime import cron
from robert_agent.work.application import proposal_digest
from tests.support.work_items import WorkItemCommandFixture


class ProposalCronTests(WorkItemCommandFixture):
    def setUp(self):
        super().setUp()
        self.config = {
            "db_path": str(self.db_path),
            "repos": [{"full_name": "example/repo"}],
            "messaging": {"enabled": True, "repos": ["example/repo"]},
            "proposer": {
                "enabled": True,
                "schedule": {
                    "enabled": True,
                    "analysis_time": "08:30",
                    "digest_time": "09:30",
                    "web_url": "https://robert.internal",
                    "worker": "default",
                    "analysis_limit": 20,
                },
            },
        }

    def add_proposal(self, identifier, repo_id="repo-1", title="Recommended work", candidates=None):
        source = {"title": title, "repository": "example/repo"}
        if candidates is not None:
            source["repository_candidates"] = candidates
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO proposals(proposal_id,source_identity,repo_id,status,version,revision,source_key,fingerprint,created_at,updated_at) "
                "VALUES(?,?,?,'pending',1,1,?,?,'2026-11-25','2026-11-25')",
                (identifier, identifier, repo_id, identifier, identifier),
            )
            conn.execute(
                "INSERT INTO proposal_revisions(proposal_id,revision,source_json,report_json,created_at) VALUES(?,1,?,?,?)",
                (
                    identifier,
                    json.dumps(source),
                    json.dumps({"recommendation": "high", "summary": "Bounded fix"}),
                    "2026-11-25",
                ),
            )

    def test_before_schedule_has_no_effect_and_daily_summary_is_deduplicated(self):
        scheduler = cron.Scheduler(self.config, "/tmp/config.yml")
        with patch.object(scheduler, "start_analysis") as start:
            scheduler.step(now="2026-11-25T00:29:59Z")
            start.assert_not_called()
        with patch.object(scheduler, "start_analysis") as start:
            scheduler.step(now="2026-11-25T00:30:00Z")
            start.assert_called_once()
        self.add_proposal("p1")
        self.assertTrue(proposal_digest.enqueue(self.config, "2026-11-25", "2026-11-25T01:30:00Z"))
        self.assertFalse(proposal_digest.enqueue(self.config, "2026-11-25", "2026-11-25T02:30:00Z"))
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute("SELECT notification_id,metadata_json FROM notifications").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "proposal-digest:2026-11-25")
        data = json.loads(rows[0][1])
        self.assertIn("Recommended work", data["summary"])
        self.assertEqual(data["result_url"], "https://robert.internal/proposals")
        self.assertEqual(data["proposal_digest_repo_ids"], ["repo-1"])

    def test_slow_analysis_does_not_delay_digest_and_reports_refreshing(self):
        scheduler = cron.Scheduler(self.config, "/tmp/config.yml")
        scheduler.process = Mock()
        scheduler.process.poll.return_value = None
        scheduler.started = cron.time.monotonic()
        with patch.object(scheduler, "start_analysis") as start:
            scheduler.step(now="2026-11-25T01:30:00Z")
        start.assert_not_called()
        with closing(sqlite3.connect(self.db_path)) as conn:
            raw = conn.execute("SELECT metadata_json FROM notifications").fetchone()[0]
        self.assertIn("分析仍在进行", json.loads(raw)["summary"])

    def test_summary_excludes_other_repositories_and_unbound_candidates_with_partial_scope(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) "
                "VALUES('repo-2','example/private','robot','main','/repo2','/worktrees2')"
            )
        self.add_proposal("private", repo_id="repo-2", title="Private title")
        self.add_proposal(
            "unbound",
            repo_id=None,
            title="Unbound secret",
            candidates=[
                {"repository": "example/repo", "label": "one"},
                {"repository": "example/private", "label": "two"},
            ],
        )
        self.assertTrue(proposal_digest.enqueue(self.config, "2026-11-25", "2026-11-25T01:30:00Z"))
        with closing(sqlite3.connect(self.db_path)) as conn:
            data = json.loads(conn.execute("SELECT metadata_json FROM notifications").fetchone()[0])
        self.assertIn("暂无推荐", data["summary"])
        self.assertNotIn("Private", data["summary"])
        self.assertNotIn("Unbound", data["summary"])

    def test_partial_analysis_retries_after_cooldown_with_a_bounded_daily_budget(self):
        day = "2026-11-25"
        for attempt in range(3):
            now = f"2026-11-25T00:{30 + attempt * 6:02}:00Z"
            self.assertTrue(proposal_digest.claim_analysis(self.config, day, now))
            proposal_digest.finish_analysis(self.config, day, now, {"ok": True, "status": "partial"})
            self.assertFalse(proposal_digest.claim_analysis(self.config, day, now))
        self.assertFalse(proposal_digest.claim_analysis(self.config, day, "2026-11-25T01:15:00Z"))

    def test_cli_accepts_separate_cron_and_once_flag(self):
        from robert_agent.cli.main import build_parser

        args = build_parser().parse_args(["cron", "--config", "/tmp/config.yml", "--once"])
        self.assertEqual((args.command, args.config, args.once), ("cron", "/tmp/config.yml", True))

    def test_scheduler_lock_blocks_a_second_process_without_running_work(self):
        from robert_agent.delivery.lock import task_publication_lock

        with task_publication_lock(self.db_path, "proposal-cron") as acquired:
            self.assertTrue(acquired)
            with patch.object(cron.Scheduler, "step") as step:
                result = cron.run(self.config, "/tmp/config.yml", once=True)
            step.assert_not_called()
            self.assertEqual(result["status"], "already_running")

    def test_completed_child_is_recorded_and_never_restarted_on_the_same_day(self):
        day, now = "2026-11-25", "2026-11-25T00:30:00Z"
        proposal_digest.claim_analysis(self.config, day, now)
        with patch.object(
            cron.proposal_scan, "scan_daily", return_value={"ok": True, "status": "completed"}, create=True
        ):
            self.assertEqual(cron.run_analysis(self.config, day)["status"], "completed")
        self.assertFalse(proposal_digest.claim_analysis(self.config, day, "2026-11-25T01:30:00Z"))

    def test_spawn_uses_canonical_child_and_deadline_without_holding_a_writer(self):
        scheduler = cron.Scheduler(self.config, "/tmp/config.yml")
        calls = []

        def launch(argv, **kwargs):
            with closing(sqlite3.connect(self.db_path, timeout=0)) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
            calls.append((argv, kwargs))
            return Mock()

        with patch.object(cron.subprocess, "Popen", side_effect=launch):
            self.assertTrue(scheduler.start_analysis("2026-11-25", "2026-11-25T00:30:00Z"))
        self.assertIn("robert_agent.runtime.cron", calls[0][0])
        self.assertTrue(calls[0][1]["start_new_session"])
        self.assertIn("_ROBERT_CONTROL_DEADLINE", calls[0][1]["env"])

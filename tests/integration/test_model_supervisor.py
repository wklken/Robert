from contextlib import closing
import json
import sqlite3
import sys

from tests.support.pr_remediation_integration import PrRemediationIntegrationFixture
from robert_agent.runtime.cycle import with_python_bin
from robert_agent.supervision import decisions
from robert_agent.work.application import remediation_requests


class ModelPrSupervisionTests(PrRemediationIntegrationFixture):
    def setUp(self):
        super().setUp()
        from robert_agent.foundation.values import github_source_id

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for old, key in (("source:issue", "github:example/backend#1"), ("source:pr", "github:example/backend!13")):
                current = github_source_id(key)
                conn.execute("UPDATE github_sources SET source_id=? WHERE source_id=?", (current, old))
                conn.execute("UPDATE workstreams SET primary_source_id=? WHERE primary_source_id=?", (current, old))

    def test_model_selects_one_repair_and_old_rules_cannot_bypass_wait(self):
        now = "2026-07-26T00:01:00+00:00"
        root_id = "github:example/backend#1"
        config = {
            "db_path": str(self.db_path),
            "repos": [self.repo],
            "supervision": {"mode": "decide", "decision": {"command": [sys.executable]}},
        }

        def collect(conn):
            return remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, self.runner(), now, config=config
            )

        def decide(conn, action):
            conn.execute("BEGIN IMMEDIATE") if not conn.in_transaction else None
            packet = decisions.packet(decisions.snapshot(conn, root_id, config, now), decisions.read(conn, root_id))
            candidate = next((c for c in packet["candidates"] if c["action"] == action), None)
            output = {
                "bundle_id": packet["bundle_id"],
                "bundle_sha256": packet["bundle_sha256"],
                "action": action,
                "candidate_id": candidate["id"] if candidate else None,
                "reason": "Resolve the current blocker",
                "evidence_ids": ["episodes"],
                "expected_progress": "Conflict clears on the current PR",
                "recheck_seconds": 300,
                "message": "",
            }
            self.assertTrue(
                decisions.approve(conn, config, packet, decisions.validate(json.dumps(output), packet), now)
            )
            conn.commit()

        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertEqual(collect(conn), [])
            decide(conn, "wait")
            self.assertEqual(collect(conn), [])
            decide(conn, "resolve_conflict")
            now = "2026-07-26T00:02:00+00:00"
            events = collect(conn)
            self.assertEqual([event["task_kind"] for event in events], ["merge_conflict_remediation"])
            # CI remains a fact, but only the model-selected conflict action runs.
            self.assertEqual(conn.execute("SELECT count(*) FROM pr_remediation_episodes").fetchone()[0], 2)

    def test_direct_dispatch_prioritizes_conflict_without_model_approval(self):
        self.repo["pr_automation"]["dispatch_mode"] = "direct"
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}, "python_bin": sys.executable}
        runtime_repo = with_python_bin(self.repo, config)
        root_id = "github:example/backend#1"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            for mergeable, expected in (
                (None, []),
                (False, ["merge_conflict_remediation"]),
                (True, ["ci_remediation"]),
            ):
                with self.subTest(mergeable=mergeable):
                    now = "2026-07-26T00:01:00+00:00"
                    events = remediation_requests.collect_pr_remediation_decisions(
                        conn, "repo:example/backend", runtime_repo, self.runner(mergeable=mergeable), now, config=config
                    )
                    self.assertEqual([event["task_kind"] for event in events], expected)
                    observation = decisions.snapshot(conn, root_id, config, now)
                    self.assertEqual(observation["candidates"], [])
                    self.assertEqual(decisions.read(conn, root_id), {})

    def test_direct_dispatch_leaves_failed_attempt_retry_to_model(self):
        self.repo["pr_automation"]["dispatch_mode"] = "direct"
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        now = "2026-07-26T00:01:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            events = remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, self.runner(), now, config=config
            )
            self.assertEqual([event["task_kind"] for event in events], ["merge_conflict_remediation"])
            conn.execute(
                "UPDATE pr_remediation_episodes SET attempt_count = 1 WHERE episode_kind = 'merge_conflict' AND status = 'open'"
            )
            self.assertEqual(
                remediation_requests.collect_pr_remediation_decisions(
                    conn, "repo:example/backend", self.repo, self.runner(), now, config=config
                ),
                [],
            )
            observation = decisions.snapshot(conn, "github:example/backend#1", config, now)
            self.assertEqual(
                [(item["action"], item["facts"]["attempt_count"]) for item in observation["candidates"]],
                [("resolve_conflict", 1)],
            )

    def test_direct_dispatch_resumes_when_late_ci_evidence_is_available(self):
        self.repo["pr_automation"]["dispatch_mode"] = "direct"
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        runner = self.runner(mergeable=True)
        command = ("gh", "run", "view", "--repo", "example/backend", "--job", "501", "--log-failed")
        runner.responses[command] = ""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            args = (conn, "repo:example/backend", self.repo, runner)
            before = remediation_requests.collect_pr_remediation_decisions(
                *args, "2026-07-26T00:01:00+00:00", config=config
            )
            self.assertEqual(before, [])
            old_episode = conn.execute(
                "SELECT episode_id, status FROM pr_remediation_episodes WHERE episode_kind = 'ci'"
            ).fetchone()
            self.assertEqual(old_episode[1], "exhausted")
            runner.responses[command] = "FAIL tests/test_api.py expected 1 got 2"
            after = remediation_requests.collect_pr_remediation_decisions(
                *args, "2026-07-26T00:03:00+00:00", config=config
            )
            self.assertEqual([event["task_kind"] for event in after], ["ci_remediation"])
            evidence = after[0]["metadata"]["remediation"]
            self.assertEqual(evidence["episode_id"], old_episode[0])
            self.assertTrue(evidence["failure_signature"])
            self.assertIn("expected 1 got 2", evidence["failure_summary"])
            self.assertEqual(
                conn.execute("SELECT first_seen_at, attempt_count FROM pr_remediation_episodes").fetchone(),
                ("2026-07-26T00:01:00+00:00", 0),
            )

    def test_late_ci_evidence_does_not_reopen_an_already_claimed_episode(self):
        self.repo["pr_automation"]["dispatch_mode"] = "direct"
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        runner = self.runner(mergeable=True)
        command = ("gh", "run", "view", "--repo", "example/backend", "--job", "501", "--log-failed")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            args = (conn, "repo:example/backend", self.repo, runner)
            first = remediation_requests.collect_pr_remediation_decisions(
                *args, "2026-07-26T00:01:00+00:00", config=config
            )
            self.assertEqual([event["task_kind"] for event in first], ["ci_remediation"])
            log = runner.responses[command]
            runner.responses[command] = ""
            self.assertEqual(
                remediation_requests.collect_pr_remediation_decisions(
                    *args, "2026-07-26T00:02:00+00:00", config=config
                ),
                [],
            )
            runner.responses[command] = log
            self.assertEqual(
                remediation_requests.collect_pr_remediation_decisions(
                    *args, "2026-07-26T00:03:00+00:00", config=config
                ),
                [],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT e.status, b.binding_status FROM pr_remediation_episodes e "
                    "JOIN execution_subjects b ON b.subject_id = e.episode_id"
                ).fetchone(),
                ("exhausted", "terminal"),
            )

    def test_direct_dispatch_leaves_unsupported_ci_visible_to_supervisor(self):
        self.repo["pr_automation"]["dispatch_mode"] = "direct"
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        runner = self.runner(mergeable=True)
        for command, response in runner.responses.items():
            if any("/actions/runs?head_sha=" in arg for arg in command):
                response["workflow_runs"][0]["conclusion"] = "cancelled"
        now = "2026-07-26T00:01:00+00:00"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertEqual(
                remediation_requests.collect_pr_remediation_decisions(
                    conn, "repo:example/backend", self.repo, runner, now, config=config
                ),
                [],
            )
            observation = decisions.snapshot(conn, "github:example/backend#1", config, now)
            self.assertEqual(observation["candidates"], [])
            self.assertEqual([item["status"] for item in observation["context"]["episodes"]], ["exhausted"])
            self.assertEqual(
                conn.execute("SELECT notification_type FROM notifications").fetchall(),
                [("pr_remediation_manual_conclusion",)],
            )

    def test_source_revision_change_invalidates_the_model_choice(self):
        now = "2026-07-26T00:01:00+00:00"
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, self.runner(), now, config=config
            )
            request = decisions.packet(decisions.snapshot(conn, "github:example/backend#1", config, now), {})
            conn.execute("UPDATE github_sources SET state='closed' WHERE source_type='pull_request'")
            self.assertFalse(decisions.approve(conn, config, request, {}, now))

    def test_cleared_conflict_and_green_ci_retract_old_model_candidates(self):
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, self.runner(), "2026-07-26T00:01:00+00:00", config=config
            )
            initial = decisions.snapshot(conn, "github:example/backend#1", config, "2026-07-26T00:01:00+00:00")
            self.assertEqual({c["action"] for c in initial["candidates"]}, {"resolve_conflict", "repair_ci"})
            runner = self.runner(mergeable=True)
            runner.responses[
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs?head_sha=head-1&per_page=100",
                    "--paginate",
                    "--slurp",
                )
            ]["workflow_runs"][0]["conclusion"] = "success"
            events = remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, runner, "2026-07-26T00:02:00+00:00", config=config
            )
            self.assertEqual(events, [])
            current = decisions.snapshot(conn, "github:example/backend#1", config, "2026-07-26T00:02:00+00:00")
            self.assertEqual(current["candidates"], [])

    def test_first_healthy_pr_scan_does_not_require_an_episode_write(self):
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        runner = self.runner(mergeable=True)
        runner.responses[
            ("gh", "api", "repos/example/backend/actions/runs?head_sha=head-1&per_page=100", "--paginate", "--slurp")
        ]["workflow_runs"][0]["conclusion"] = "success"
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertEqual(
                remediation_requests.collect_pr_remediation_decisions(
                    conn, "repo:example/backend", self.repo, runner, "2026-07-26T00:01:00+00:00", config=config
                ),
                [],
            )

    def test_multiple_pr_scan_never_commits_partial_candidate_withdrawal(self):
        config = {"repos": [self.repo], "supervision": {"mode": "decide"}}
        runner = self.runner()
        runner.responses[("gh", "api", "repos/example/backend/pulls/14")] = runner.responses[
            ("gh", "api", "repos/example/backend/pulls/13")
        ]
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO github_sources(source_id,repo_id,source_key,source_type,number,title,state,author_login) VALUES ('source14','repo:example/backend','github:example/backend!14','pull_request',14,'Fix','open','robert-bot')"
            )
            conn.execute(
                "INSERT INTO workstreams(workstream_id,repo_id,primary_source_id,origin_workstream_id,lifecycle,created_at,updated_at) VALUES ('github:example/backend!14','repo:example/backend','source14','github:example/backend#1','completed','2026-07-26','2026-07-26')"
            )
            remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, runner, "2026-07-26T00:01:00+00:00", config=config
            )
            conn.commit()
            checks = []

            def observing_runner(args, **kwargs):
                if args[-1] == "repos/example/backend/pulls/14":
                    with closing(sqlite3.connect(self.db_path)) as reader:
                        candidates = [
                            json.loads(row[0])
                            for row in reader.execute(
                                "SELECT metadata_json FROM supervision_checkpoints WHERE scope_type='decision_candidate'"
                            )
                        ]
                        self.assertTrue(candidates)
                        self.assertFalse(any(c.get("withdrawn") for c in candidates))
                        checks.append(True)
                return runner(args, **kwargs)

            remediation_requests.collect_pr_remediation_decisions(
                conn, "repo:example/backend", self.repo, observing_runner, "2026-07-26T00:02:00+00:00", config=config
            )
            self.assertEqual(checks, [True])

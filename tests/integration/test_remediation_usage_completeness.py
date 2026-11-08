from contextlib import closing
import json
import sqlite3
import unittest

from robert_agent.work.application import remediation_requests
from robert_agent.workflows import pr_remediation
from tests.support.pr_remediation_integration import PrRemediationIntegrationFixture


REPO = "repo:example/backend"
NOW = "2026-07-26T00:01:00Z"
TOKEN_LIMIT = {"max_total_tokens": 100}
COST_LIMIT = {"max_cost_usd": 0.001}
BOTH_LIMITS = TOKEN_LIMIT | COST_LIMIT
TOKENS_ONLY = {"usage": {"input_tokens": 20, "output_tokens": 5}}
COST_ONLY = {"total_cost_usd": 0.0001}
COMPLETE_USAGE = TOKENS_ONLY | {"total_cost_usd": 0.0}


class RemediationUsageCompletenessTests(unittest.TestCase):
    def assert_retry_budget(self, limits, attempt_logs, *, reason=""):
        fixture = PrRemediationIntegrationFixture()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.repo["pr_automation"]["conflict"]["enabled"] = False
        policy = fixture.repo["pr_automation"]["ci"]
        policy.pop("max_total_tokens")
        policy.pop("max_cost_usd")
        policy.update(limits, max_attempts=4)
        with closing(sqlite3.connect(fixture.db_path)) as conn, conn:
            events = remediation_requests.collect_pr_remediation_decisions(
                conn, REPO, fixture.repo, fixture.runner(mergeable=True), NOW
            )
            self.assertEqual([event["task_kind"] for event in events], ["ci_remediation"])
            event = events[0]
            episode_id = event["metadata"]["remediation"]["episode_id"]
            for index, payload in enumerate(attempt_logs, start=1):
                log = fixture.db_path.parent / f"attempt-{index}.stdout"
                log.write_text(json.dumps({"type": "turn.completed", **payload}) + "\n", encoding="utf-8")
                task_id = f"task-{index}"
                conn.execute(
                    "INSERT INTO tasks(task_id,workstream_id,task_kind,lifecycle,created_at,updated_at,metadata_json)"
                    " VALUES (?,?,'ci_remediation','failed',?,?,?)",
                    (task_id, event["workstream_id"], NOW, NOW, json.dumps({"remediation_episode_id": episode_id})),
                )
                conn.execute(
                    "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES (?,?,1,'failed',?)",
                    (f"attempt-{index}", task_id, json.dumps({"dispatch": {"stdout_path": str(log)}})),
                )
                self.assertTrue(
                    pr_remediation.record_task_started(conn, episode_id=episode_id, task_id=task_id, now=NOW)
                )
                self.assertTrue(
                    pr_remediation.record_task_failure(conn, task_id=task_id, reason="worker_failed", now=NOW)
                )

            episode = pr_remediation.load_episode(conn, episode_id)
            self.assertEqual(episode["attempt_count"], len(attempt_logs))
            budget = pr_remediation.budget_status(conn, episode=episode, policy=policy, now=NOW)
            selected = pr_remediation.next_system_decisions(conn, repo={**fixture.repo, "repo_id": REPO}, now=NOW)
            outcome = pr_remediation.load_episode(conn, episode_id)

        self.assertEqual(
            {
                "budget_reason": budget["reason"],
                "retry_eligible": [entry["episode_id"] for entry in selected],
                "episode_status": outcome["status"],
            },
            {
                "budget_reason": reason,
                "retry_eligible": [] if reason else [episode_id],
                "episode_status": "exhausted" if reason else "open",
            },
        )

    def test_each_attempt_must_measure_every_configured_dimension(self):
        for missing, partial in (
            ("cost", TOKENS_ONLY),
            ("all tokens", COST_ONLY),
            ("output tokens", COST_ONLY | {"usage": {"input_tokens": 20}}),
        ):
            with self.subTest(missing=missing):
                self.assert_retry_budget(BOTH_LIMITS, [COMPLETE_USAGE, partial], reason="usage_unavailable")

    def test_invalid_cost_does_not_count_as_measured(self):
        for kind, cost in (
            ("malformed", "invalid"),
            ("negative", -0.0001),
            ("nan", float("nan")),
            ("infinite", float("inf")),
            ("boolean", False),
            ("oversized integer", 10**400),
        ):
            with self.subTest(cost_kind=kind):
                self.assert_retry_budget(
                    BOTH_LIMITS, [TOKENS_ONLY | {"total_cost_usd": cost}], reason="usage_unavailable"
                )

    def test_complete_usage_applies_only_configured_limits(self):
        for scenario, limits, payload, reason in (
            ("explicit zero cost", BOTH_LIMITS, COMPLETE_USAGE, ""),
            ("over cost limit", BOTH_LIMITS, COMPLETE_USAGE | {"total_cost_usd": 0.0011}, "max_cost_usd"),
            ("only token limit", TOKEN_LIMIT, TOKENS_ONLY, ""),
            ("only cost limit", COST_LIMIT, COST_ONLY, ""),
        ):
            with self.subTest(scenario=scenario):
                self.assert_retry_budget(limits, [payload], reason=reason)

    def test_extremely_large_integer_tokens_exhaust_budget_without_crashing(self):
        self.assert_retry_budget(
            TOKEN_LIMIT, [{"usage": {"input_tokens": 10**400, "output_tokens": 0}}], reason="max_total_tokens"
        )

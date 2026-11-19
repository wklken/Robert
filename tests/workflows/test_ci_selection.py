import subprocess
import unittest

from robert_agent.workflows import pr_health
from tests.workflows.test_pr_health import FixtureRunner


PATH = "dynamic/github-code-scanning/codeql"


def observation(name="backend ci", **changes):
    return {
        "source_kind": "workflow_run",
        "producer_id": "workflow:51",
        "external_id": "101",
        "attempt_no": 1,
        "check_name": name,
        "status": "completed",
        "conclusion": "failure",
        "evidence_status": "available",
        "failure_summary": "Test failed",
        "failure_signature": "signature",
        **changes,
    }


class CISelectionTests(unittest.TestCase):
    def test_observed_mode_repairs_failed_component_without_untriggered_workflows(self):
        result = pr_health.summarize_ci([observation()], ["backend ci", "frontend ci"], check_mode="observed")
        self.assertEqual(result["status"], "failing", result)
        self.assertEqual(result["failure_signature"], "signature")

    def test_observed_mode_waits_for_selected_running_checks_and_empty_observations(self):
        for rows in (
            [],
            [observation("unselected ci")],
            [observation(), observation("frontend ci", status="in_progress", conclusion=None)],
        ):
            with self.subTest(rows=rows):
                result = pr_health.summarize_ci(rows, ["backend ci", "frontend ci"], check_mode="observed")
                self.assertEqual(result["status"], "waiting", result)

    def test_observed_mode_preserves_failure_evidence_and_manual_gates(self):
        for changes, reason in (
            ({"evidence_status": "unavailable"}, "failure_evidence_unavailable"),
            ({"conclusion": "timed_out"}, "manual_conclusion"),
        ):
            with self.subTest(changes=changes):
                result = pr_health.summarize_ci(
                    [observation(**changes)], ["backend ci", "frontend ci"], check_mode="observed"
                )
                self.assertEqual(result["status"], "manual", result)
                self.assertEqual(result["reason"], reason)

    def test_default_mode_still_requires_all_named_and_path_selected_workflows(self):
        result = pr_health.summarize_ci([observation()], ["backend ci", "frontend ci"])
        self.assertEqual(result["status"], "waiting")
        result = pr_health.summarize_ci([observation()], ["backend ci"], workflow_path_allowlist=[PATH])
        self.assertEqual(result["status"], "waiting")
        result = pr_health.summarize_ci([observation("PR #16", workflow_path=PATH)], [], workflow_path_allowlist=[PATH])
        self.assertEqual(result["status"], "failing")

    def test_latest_same_producer_path_run_clears_failure_after_dynamic_name_changes(self):
        old = observation("PR #16", workflow_path=PATH, completed_at="2026-11-19T00:00:00Z")
        new = observation(
            "PR #17", workflow_path=PATH, external_id="102", conclusion="success", completed_at="2026-11-19T00:01:00Z"
        )
        result = pr_health.summarize_ci([old, new], [], workflow_path_allowlist=[PATH], check_mode="observed")
        self.assertEqual(result["status"], "green", result)
        self.assertEqual(result["observations"], [new])
        other = {**new, "producer_id": "workflow:52"}
        result = pr_health.summarize_ci([old, other], [], workflow_path_allowlist=[PATH], check_mode="observed")
        self.assertEqual(result["status"], "failing", result)

    def test_path_selector_does_not_select_external_check_with_the_same_path(self):
        result = pr_health.summarize_ci(
            [observation("PR #16", source_kind="check_run", workflow_path=PATH)],
            [],
            workflow_path_allowlist=[PATH],
            check_mode="observed",
        )
        self.assertEqual(result["status"], "waiting", result)

    def test_collects_dynamic_workflow_by_exact_path_and_current_head(self):
        run = {
            "id": 101,
            "name": "PR #16",
            "path": PATH,
            "workflow_id": 51,
            "head_sha": "head",
            "status": "completed",
            "conclusion": "failure",
        }
        runner = FixtureRunner(
            {
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs?head_sha=head&per_page=100",
                    "--paginate",
                    "--slurp",
                ): {
                    "workflow_runs": [
                        run,
                        {**run, "head_sha": "old-head"},
                        {**run, "id": 102, "path": PATH + "-other"},
                    ],
                },
                ("gh", "api", "repos/example/backend/commits/head/check-runs?per_page=100", "--paginate", "--slurp"): {
                    "check_runs": []
                },
                ("gh", "api", "repos/example/backend/actions/runs/101/jobs?per_page=100"): {
                    "jobs": [{"id": 501, "name": "Analyze", "conclusion": "failure"}]
                },
                ("gh", "run", "view", "--repo", "example/backend", "--job", "501", "--log-failed"): "Test failed",
            }
        )
        rows = pr_health.collect_ci_observations(
            "example/backend",
            "head",
            check_allowlist=[],
            workflow_path_allowlist=[PATH],
            max_summary_chars=200,
            runner=runner,
        )
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["check_name"], "PR #16")
        self.assertEqual(rows[0]["workflow_path"], PATH)
        self.assertEqual(rows[0]["evidence_status"], "available")

    def test_failed_collection_cannot_turn_partial_success_into_green(self):
        for failed_endpoint in ("workflows", "checks"):
            with self.subTest(endpoint=failed_endpoint):
                failure = subprocess.CalledProcessError(1, ["gh", "api"])
                runner = FixtureRunner(
                    {
                        (
                            "gh",
                            "api",
                            "repos/example/backend/actions/runs?head_sha=head&per_page=100",
                            "--paginate",
                            "--slurp",
                        ): failure
                        if failed_endpoint == "workflows"
                        else {
                            "workflow_runs": [
                                {
                                    "id": 101,
                                    "name": "backend ci",
                                    "head_sha": "head",
                                    "status": "completed",
                                    "conclusion": "success",
                                }
                            ]
                        },
                        (
                            "gh",
                            "api",
                            "repos/example/backend/commits/head/check-runs?per_page=100",
                            "--paginate",
                            "--slurp",
                        ): failure
                        if failed_endpoint == "checks"
                        else {
                            "check_runs": [
                                {
                                    "id": 501,
                                    "name": "backend ci",
                                    "app": {"slug": "external-ci"},
                                    "status": "completed",
                                    "conclusion": "success",
                                }
                            ]
                        },
                    }
                )
                rows = pr_health.collect_ci_observations(
                    "example/backend", "head", check_allowlist=["backend ci"], max_summary_chars=200, runner=runner
                )
                self.assertEqual(
                    pr_health.summarize_ci(rows, ["backend ci"], check_mode="observed")["status"], "waiting"
                )

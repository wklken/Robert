import unittest

from robert_agent.workflows import pr_health
from tests.workflows.test_pr_health import FixtureRunner


REPO = "example/backend"
HEAD = "head-1"
CHECK = "unit"
EARLIER = "2026-07-26T00:01:00Z"
LATER = "2026-07-26T00:02:00Z"


def workflow_run(run_id, conclusion, *, workflow_id=None):
    payload = {
        "id": run_id,
        "run_attempt": 1,
        "name": CHECK,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": conclusion,
        "updated_at": LATER if conclusion == "success" else EARLIER,
    }
    if workflow_id is not None:
        payload["workflow_id"] = workflow_id
    return payload


def check_run(check_id, conclusion, *, app=None):
    payload = {
        "id": check_id,
        "name": CHECK,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": LATER if conclusion == "success" else EARLIER,
        "output": {"summary": "Test failed"} if conclusion == "failure" else {},
    }
    if app is not None:
        payload["app"] = app
    return payload


class CIProducerIdentityTests(unittest.TestCase):
    def summarize(self, workflow_runs, check_runs):
        responses = {
            ("gh", "api", f"repos/{REPO}/actions/runs?head_sha={HEAD}&per_page=100", "--paginate", "--slurp"): {
                "workflow_runs": workflow_runs
            },
            ("gh", "api", f"repos/{REPO}/commits/{HEAD}/check-runs?per_page=100", "--paginate", "--slurp"): {
                "check_runs": check_runs
            },
        }
        for run in workflow_runs:
            if run["conclusion"] == "failure":
                job_id = run["id"] + 1000
                responses[("gh", "api", f"repos/{REPO}/actions/runs/{run['id']}/jobs?per_page=100")] = {
                    "jobs": [{"id": job_id, "name": "unit", "status": "completed", "conclusion": "failure"}],
                }
                responses[("gh", "api", f"repos/{REPO}/actions/jobs/{job_id}/logs")] = "Test failed"
        for check in check_runs:
            if check["conclusion"] == "failure":
                responses[("gh", "api", f"repos/{REPO}/check-runs/{check['id']}/annotations?per_page=100")] = []

        observations = pr_health.collect_ci_observations(
            REPO, HEAD, check_allowlist=[CHECK], max_summary_chars=200, runner=FixtureRunner(responses)
        )
        self.assertCountEqual([row["conclusion"] for row in observations], ["failure", "success"])
        return pr_health.summarize_ci(observations, [CHECK])

    def assert_failure_survives(self, workflow_runs, check_runs, failed_kind, failed_id):
        summary = self.summarize(workflow_runs, check_runs)
        self.assertEqual(summary["status"], "failing", summary)
        self.assertEqual(
            [(failure["source_kind"], failure["external_id"]) for failure in summary["failures"]],
            [(failed_kind, str(failed_id))],
        )

    def test_same_name_success_from_a_different_producer_preserves_failure(self):
        first_workflow = workflow_run(101, "failure", workflow_id=51)
        first_check = check_run(201, "failure", app={"id": 51, "slug": "external-ci"})
        for scenario, workflows, checks, failed_kind, failed_id in (
            (
                "workflow and app with the same producer ID",
                [first_workflow],
                [check_run(202, "success", app={"id": 51, "slug": "external-ci"})],
                "workflow_run",
                101,
            ),
            (
                "two workflows",
                [first_workflow, workflow_run(102, "success", workflow_id=52)],
                [],
                "workflow_run",
                101,
            ),
            (
                "two apps",
                [],
                [first_check, check_run(202, "success", app={"id": 52, "slug": "other-ci"})],
                "check_run",
                201,
            ),
        ):
            with self.subTest(scenario=scenario):
                self.assert_failure_survives(workflows, checks, failed_kind, failed_id)

    def test_missing_producer_keeps_distinct_external_observations(self):
        with self.subTest(source="workflow_run"):
            self.assert_failure_survives(
                [workflow_run(101, "failure"), workflow_run(102, "success")], [], "workflow_run", 101
            )
        with self.subTest(source="check_run"):
            self.assert_failure_survives([], [check_run(201, "failure"), check_run(202, "success")], "check_run", 201)

    def test_later_success_from_the_same_producer_clears_failure(self):
        with self.subTest(source="workflow_run"):
            summary = self.summarize(
                [workflow_run(101, "failure", workflow_id=51), workflow_run(102, "success", workflow_id=51)], []
            )
            self.assertEqual(summary["status"], "green", summary)
            self.assertEqual([row["external_id"] for row in summary["observations"]], ["102"])

        for identity, app in (("app ID", {"id": 51}), ("app slug", {"slug": "external-ci"})):
            with self.subTest(source="check_run", identity=identity):
                summary = self.summarize([], [check_run(201, "failure", app=app), check_run(202, "success", app=app)])
                self.assertEqual(summary["status"], "green", summary)
                self.assertEqual([row["external_id"] for row in summary["observations"]], ["202"])

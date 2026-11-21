import json
import subprocess
import unittest


class FixtureRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        key = tuple(args)
        response = self.responses.get(key)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise AssertionError(f"unexpected command: {args}")
        if isinstance(response, str):
            stdout = response
        else:
            stdout = json.dumps(response)
        return subprocess.CompletedProcess(args, 0, stdout, "")


class PrHealthTests(unittest.TestCase):
    def test_fetch_pr_snapshot_normalizes_exact_head_and_base(self):
        from robert_agent.workflows import pr_health

        runner = FixtureRunner(
            {
                ("gh", "api", "repos/example/backend/pulls/13"): {
                    "state": "open",
                    "merged": False,
                    "mergeable": False,
                    "head": {
                        "sha": "head-1",
                        "ref": "codex/fix",
                        "repo": {"full_name": "example/backend"},
                    },
                    "base": {
                        "sha": "old-base",
                        "ref": "release/#test",
                    },
                    "user": {"login": "robert-bot"},
                    "html_url": "https://github.com/example/backend/pull/13",
                    "updated_at": "2026-07-26T00:00:00Z",
                },
                ("gh", "api", "repos/example/backend/git/ref/heads/release/%23test"): {
                    "ref": "refs/heads/release/#test",
                    "object": {"sha": "base-1"},
                },
            }
        )

        snapshot = pr_health.fetch_pr_snapshot(
            "example/backend",
            13,
            runner=runner,
        )

        self.assertEqual(
            snapshot,
            {
                "state": "open",
                "merged": False,
                "mergeable": False,
                "head_sha": "head-1",
                "head_ref": "codex/fix",
                "head_repo_full_name": "example/backend",
                "base_sha": "base-1",
                "base_ref": "release/#test",
                "author_login": "robert-bot",
                "html_url": "https://github.com/example/backend/pull/13",
                "updated_at": "2026-07-26T00:00:00Z",
            },
        )

    def test_fetch_pr_snapshot_preserves_unknown_mergeability_and_read_failure(self):
        from robert_agent.workflows import pr_health

        payload = {
            "state": "open",
            "merged_at": None,
            "mergeable": None,
            "head": {
                "sha": "head",
                "ref": "topic",
                "repo": {"full_name": "fork/backend"},
            },
            "base": {"sha": "base", "ref": "main"},
            "user": {"login": "contributor"},
        }
        snapshot = pr_health.fetch_pr_snapshot(
            "example/backend",
            14,
            runner=FixtureRunner(
                {
                    ("gh", "api", "repos/example/backend/pulls/14"): payload,
                    ("gh", "api", "repos/example/backend/git/ref/heads/main"): {
                        "ref": "refs/heads/main",
                        "object": {"sha": "base"},
                    },
                }
            ),
        )
        self.assertIsNone(snapshot["mergeable"])
        self.assertEqual(snapshot["head_repo_full_name"], "fork/backend")

        failed = subprocess.CalledProcessError(1, ["gh", "api"])
        self.assertIsNone(
            pr_health.fetch_pr_snapshot(
                "example/backend",
                14,
                runner=FixtureRunner(
                    {
                        (
                            "gh",
                            "api",
                            "repos/example/backend/pulls/14",
                        ): failed
                    }
                ),
            )
        )

    def test_fetch_pr_snapshot_requires_an_observed_base_branch(self):
        from robert_agent.workflows import pr_health

        runner = FixtureRunner(
            {
                ("gh", "api", "repos/example/backend/pulls/13"): {
                    "state": "open",
                    "mergeable": False,
                    "head": {"sha": "head"},
                    "base": {"ref": "main", "sha": "possibly-stale"},
                },
                ("gh", "api", "repos/example/backend/git/ref/heads/main"): subprocess.CalledProcessError(
                    1, ["gh", "api"]
                ),
            }
        )
        self.assertIsNone(pr_health.fetch_pr_snapshot("example/backend", 13, runner=runner))

    def test_collect_ci_observations_aggregates_redacted_actions_failure(self):
        from robert_agent.workflows import pr_health

        responses = {
            (
                "gh",
                "api",
                "repos/example/backend/actions/runs?head_sha=head-1&per_page=100",
                "--paginate",
                "--slurp",
            ): {
                "workflow_runs": [
                    {
                        "id": 101,
                        "run_attempt": 2,
                        "name": "unit",
                        "head_sha": "head-1",
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://github.com/run/101",
                        "updated_at": "2026-07-26T00:01:00Z",
                    },
                    {
                        "id": 102,
                        "run_attempt": 1,
                        "name": "not-allowed",
                        "head_sha": "head-1",
                        "status": "completed",
                        "conclusion": "failure",
                    },
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
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            },
            (
                "gh",
                "run",
                "view",
                "--repo",
                "example/backend",
                "--job",
                "501",
                "--log-failed",
            ): (
                "\x1b[31m2026-07-26T00:00:01Z FAIL /home/runner/work/backend/tests/test_api.py: expected 1 got 2\x1b[0m"
            ),
            (
                "gh",
                "api",
                "repos/example/backend/commits/head-1/check-runs?per_page=100",
                "--paginate",
                "--slurp",
            ): {"check_runs": []},
        }
        runner = FixtureRunner(responses)

        observations = pr_health.collect_ci_observations(
            "example/backend",
            "head-1",
            check_allowlist=["unit"],
            max_summary_chars=200,
            runner=runner,
        )

        self.assertEqual(len(observations), 1)
        observation = observations[0]
        self.assertEqual(observation["source_kind"], "workflow_run")
        self.assertEqual(observation["external_id"], "101")
        self.assertEqual(observation["attempt_no"], 2)
        self.assertEqual(observation["check_name"], "unit")
        self.assertEqual(observation["conclusion"], "failure")
        self.assertEqual(observation["evidence_status"], "available")
        self.assertIn("expected 1 got 2", observation["failure_summary"])
        self.assertIn("<local-path>", observation["failure_summary"])
        self.assertNotIn("\x1b", observation["failure_summary"])
        self.assertTrue(observation["failure_signature"])
        commands = [call[0] for call in runner.calls]
        self.assertNotIn(
            [
                "gh",
                "api",
                "repos/example/backend/actions/runs/102/jobs?per_page=100",
            ],
            commands,
        )

    def test_scanned_ci_evidence_preserves_redaction_failure_on_replay(self):
        from robert_agent.adapters.github import recording
        from robert_agent.workflows import pr_health

        log = "password = audit-fixture-placeholder"
        repo = "example/backend"
        runner = FixtureRunner(
            {
                ("gh", "api", f"repos/{repo}/actions/runs/1/jobs?per_page=100"): {
                    "jobs": [
                        {
                            "id": 7,
                            "conclusion": "failure",
                            "steps": [{"number": 3, "conclusion": "failure", "name": "run tests"}],
                        }
                    ]
                },
                ("gh", "run", "view", "--repo", repo, "--job", "7", "--log-failed"): log,
            }
        )
        recorder = recording.RecordingRunner(runner, max_job_log_chars=100)
        workflow = {"id": 1, "name": "unit", "status": "completed", "conclusion": "failure"}
        direct = pr_health._workflow_observation(repo, workflow, max_summary_chars=100, runner=recorder)
        replay = pr_health._workflow_observation(
            repo, workflow, max_summary_chars=100, runner=recording.ReplayRunner(recorder.responses)
        )

        self.assertEqual(direct["evidence_status"], "blocked_secret")
        self.assertEqual(replay["evidence_status"], "blocked_secret")
        self.assertEqual(replay["failure_summary"], "")
        self.assertEqual(replay["failed_jobs"], [{"job_id": 7, "failed_step_numbers": [3]}])
        self.assertNotIn(log, json.dumps(recorder.responses))
        summary = pr_health.summarize_ci([replay], ["unit"])
        self.assertEqual(summary["status"], "manual")
        self.assertEqual(summary["reason"], "failure_evidence_unavailable")

    def test_collect_ci_observations_marks_workflow_unavailable_when_any_failed_job_log_is_unreadable(
        self,
    ):
        from robert_agent.workflows import pr_health

        responses = {
            (
                "gh",
                "api",
                "repos/example/backend/actions/runs?head_sha=head-1&per_page=100",
                "--paginate",
                "--slurp",
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
                        "updated_at": "2026-07-26T00:01:00Z",
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
                        "name": "python-3.11",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                    {
                        "id": 502,
                        "name": "python-3.12",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ]
            },
            (
                "gh",
                "run",
                "view",
                "--repo",
                "example/backend",
                "--job",
                "501",
                "--log-failed",
            ): "FAIL tests/test_api.py expected 1 got 2",
            (
                "gh",
                "run",
                "view",
                "--repo",
                "example/backend",
                "--job",
                "502",
                "--log-failed",
            ): subprocess.CalledProcessError(1, ["gh", "api"]),
            (
                "gh",
                "api",
                "repos/example/backend/commits/head-1/check-runs?per_page=100",
                "--paginate",
                "--slurp",
            ): {"check_runs": []},
        }

        observations = pr_health.collect_ci_observations(
            "example/backend",
            "head-1",
            check_allowlist=["unit"],
            max_summary_chars=200,
            runner=FixtureRunner(responses),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["evidence_status"], "unavailable")
        self.assertIn("expected 1 got 2", observations[0]["failure_summary"])

    def test_collect_ci_observations_reads_external_check_output_and_annotations(self):
        from robert_agent.workflows import pr_health

        runner = FixtureRunner(
            {
                (
                    "gh",
                    "api",
                    "repos/example/backend/actions/runs?head_sha=head-1&per_page=100",
                    "--paginate",
                    "--slurp",
                ): {"workflow_runs": []},
                (
                    "gh",
                    "api",
                    "repos/example/backend/commits/head-1/check-runs?per_page=100",
                    "--paginate",
                    "--slurp",
                ): {
                    "check_runs": [
                        {
                            "id": 201,
                            "name": "external",
                            "status": "completed",
                            "conclusion": "failure",
                            "details_url": "https://ci.example/check/201",
                            "completed_at": "2026-07-26T00:02:00Z",
                            "app": {"slug": "external-ci"},
                            "output": {
                                "title": "Tests failed",
                                "summary": "3 failures",
                                "text": "see annotations",
                            },
                        },
                        {
                            "id": 202,
                            "name": "external",
                            "status": "completed",
                            "conclusion": "failure",
                            "app": {"slug": "github-actions"},
                        },
                    ]
                },
                (
                    "gh",
                    "api",
                    "repos/example/backend/check-runs/201/annotations?per_page=100",
                ): [
                    {
                        "path": "tests/test_api.py",
                        "start_line": 17,
                        "message": "expected 1 got 2",
                    }
                ],
            }
        )

        observations = pr_health.collect_ci_observations(
            "example/backend",
            "head-1",
            check_allowlist=["external"],
            max_summary_chars=500,
            runner=runner,
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["source_kind"], "check_run")
        self.assertIn("Tests failed", observations[0]["failure_summary"])
        self.assertIn("tests/test_api.py:17", observations[0]["failure_summary"])
        self.assertIn("expected 1 got 2", observations[0]["failure_summary"])

    def test_summarize_ci_distinguishes_waiting_green_failing_and_manual(self):
        from robert_agent.workflows import pr_health

        success = {
            "check_name": "unit",
            "status": "completed",
            "conclusion": "success",
        }
        self.assertEqual(
            pr_health.summarize_ci([success], ["unit", "lint"])["status"],
            "waiting",
        )
        self.assertEqual(
            pr_health.summarize_ci(
                [
                    success,
                    {
                        "check_name": "lint",
                        "status": "completed",
                        "conclusion": "neutral",
                    },
                ],
                ["unit", "lint"],
            )["status"],
            "green",
        )
        failing = {
            "check_name": "unit",
            "status": "completed",
            "conclusion": "failure",
            "evidence_status": "available",
            "failure_summary": "assertion failed",
            "failure_signature": "sig",
        }
        summary = pr_health.summarize_ci([failing], ["unit"])
        self.assertEqual(summary["status"], "failing")
        self.assertEqual(summary["failure_signature"], "sig")

        manual = pr_health.summarize_ci(
            [
                {
                    "check_name": "unit",
                    "status": "completed",
                    "conclusion": "timed_out",
                }
            ],
            ["unit"],
        )
        self.assertEqual(manual["status"], "manual")
        self.assertEqual(manual["manual_conclusions"], ["timed_out"])

        no_evidence = pr_health.summarize_ci(
            [{**failing, "evidence_status": "blocked_secret"}],
            ["unit"],
        )
        self.assertEqual(no_evidence["status"], "manual")
        self.assertEqual(no_evidence["reason"], "failure_evidence_unavailable")

    def test_summarize_ci_uses_freshest_same_name_observation(self):
        from robert_agent.workflows import pr_health

        newer_success = {
            "external_id": "202",
            "attempt_no": 1,
            "check_name": "unit",
            "status": "completed",
            "conclusion": "success",
            "completed_at": "2026-07-26T01:00:00Z",
        }
        older_failure = {
            "external_id": "101",
            "attempt_no": 1,
            "check_name": "unit",
            "status": "completed",
            "conclusion": "failure",
            "completed_at": "2026-07-26T00:00:00Z",
            "evidence_status": "available",
            "failure_summary": "old failure",
        }

        summary = pr_health.summarize_ci(
            [newer_success, older_failure],
            ["unit"],
        )

        self.assertEqual(summary["status"], "green")
        self.assertEqual(summary["observations"], [newer_success])

    def test_summarize_ci_uses_latest_rerun_attempt_when_time_matches(self):
        from robert_agent.workflows import pr_health

        summary = pr_health.summarize_ci(
            [
                {
                    "external_id": "101",
                    "attempt_no": 1,
                    "check_name": "unit",
                    "status": "completed",
                    "conclusion": "failure",
                    "completed_at": "2026-07-26T00:00:00Z",
                    "evidence_status": "available",
                    "failure_summary": "first attempt",
                },
                {
                    "external_id": "101",
                    "attempt_no": 2,
                    "check_name": "unit",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-07-26T00:00:00Z",
                },
            ],
            ["unit"],
        )

        self.assertEqual(summary["status"], "green")
        self.assertEqual(summary["observations"][0]["attempt_no"], 2)

    def test_failure_signature_ignores_volatile_text(self):
        from robert_agent.workflows import pr_health

        first = pr_health.failure_signature(
            [
                {
                    "check_name": "unit",
                    "failure_summary": (
                        "2026-07-26T00:00:01Z \x1b[31mFAIL /home/runner/work/backend/test.py expected 1 got 2\x1b[0m"
                    ),
                }
            ]
        )
        second = pr_health.failure_signature(
            [
                {
                    "check_name": "unit",
                    "failure_summary": ("2026-07-27T08:09:10Z FAIL /Users/dev/project/test.py expected 1 got 2"),
                }
            ]
        )
        changed = pr_health.failure_signature(
            [
                {
                    "check_name": "unit",
                    "failure_summary": "FAIL test.py expected 1 got 3",
                }
            ]
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_ci_observations_include_later_pages(self):
        from robert_agent.workflows import pr_health

        def runner(args, **kwargs):
            self.assertIn("--paginate", args)
            self.assertIn("--slurp", args)
            if "/actions/runs?" in args[2]:
                payload = [
                    {"workflow_runs": []},
                    {
                        "workflow_runs": [
                            {
                                "id": 2,
                                "head_sha": "sha",
                                "name": "required",
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    },
                ]
            else:
                payload = [
                    {"check_runs": []},
                    {
                        "check_runs": [
                            {
                                "id": 3,
                                "name": "external",
                                "status": "completed",
                                "conclusion": "success",
                                "app": {"slug": "checks"},
                            }
                        ]
                    },
                ]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        observations = pr_health.collect_ci_observations(
            "x/y", "sha", check_allowlist=["required", "external"], max_summary_chars=100, runner=runner
        )
        self.assertEqual({row["external_id"] for row in observations}, {"2", "3"})


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT
import json


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = PACKAGE_ROOT


class _Completed:
    returncode = 0
    stderr = ""

    def __init__(self, stdout):
        self.stdout = stdout


class AcceptanceSuiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.yml"
        self.config_path.write_text("{}", encoding="utf-8")

    def test_acceptance_suite_reports_safe_checks_and_production_write_gap(self):
        from robert_agent import acceptance_suite
        calls = []

        def passing(name):
            def check(**kwargs):
                calls.append((name, kwargs))
                return {"ok": True, "status": "completed", "name": name}
            return check

        result = acceptance_suite.acceptance_suite(
            self.config_path,
            workspace_dir=self.root / "suite",
            checks={
                "preflight": passing("preflight"),
                "live_discovery": passing("live_discovery"),
                "controlled_e2e": passing("controlled_e2e"),
                "live_worktree": passing("live_worktree"),
                "publish_dedupe": passing("publish_dedupe"),
                "live_worker": passing("live_worker"),
            },
        )

        self.assertFalse(result["ok"], result)
        self.assertTrue(result["safe_acceptance_ok"], result)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["production_write"]["status"], "required")
        self.assertEqual(result["checks"]["live_worker"]["status"], "skipped")
        self.assertEqual(
            [name for name, _kwargs in calls],
            ["preflight", "live_discovery", "controlled_e2e", "live_worktree", "publish_dedupe"],
        )

    def test_acceptance_suite_can_include_live_worker_check(self):
        from robert_agent import acceptance_suite
        calls = []

        def passing(name):
            def check(**kwargs):
                calls.append(name)
                return {"ok": True, "status": "completed", "name": name}
            return check

        result = acceptance_suite.acceptance_suite(
            self.config_path,
            workspace_dir=self.root / "suite",
            include_live_worker=True,
            checks={
                "preflight": passing("preflight"),
                "live_discovery": passing("live_discovery"),
                "controlled_e2e": passing("controlled_e2e"),
                "live_worktree": passing("live_worktree"),
                "publish_dedupe": passing("publish_dedupe"),
                "live_worker": passing("live_worker"),
            },
        )

        self.assertIn("live_worker", calls)
        self.assertEqual(result["checks"]["live_worker"]["status"], "completed")
        self.assertFalse(result["ok"], result)

    def test_acceptance_suite_can_plan_production_write_canary_without_writing(self):
        from robert_agent import acceptance_suite
        def passing(name):
            def check(**kwargs):
                return {"ok": True, "status": "completed", "name": name}
            return check

        result = acceptance_suite.acceptance_suite(
            self.config_path,
            workspace_dir=self.root / "suite",
            production_canary_target_url="https://github.com/x/y/issues/1",
            production_canary_marker_id="marker-suite",
            checks={
                "preflight": passing("preflight"),
                "live_discovery": passing("live_discovery"),
                "controlled_e2e": passing("controlled_e2e"),
                "live_worktree": passing("live_worktree"),
                "publish_dedupe": passing("publish_dedupe"),
                "live_worker": passing("live_worker"),
            },
        )

        self.assertEqual(result["production_write"]["status"], "required")
        self.assertEqual(result["production_write"]["canary_plan"]["status"], "planned")
        self.assertFalse(result["production_write"]["canary_plan"]["write_confirmed"])
        self.assertIn("--confirm-github-write", result["production_write"]["canary_command"])

    def test_acceptance_suite_accepts_verified_production_canary_evidence(self):
        from robert_agent import acceptance_suite
        calls = []

        def passing(name):
            def check(**kwargs):
                return {"ok": True, "status": "completed", "name": name}
            return check

        def fake_run(command, **kwargs):
            calls.append(list(command))
            self.assertEqual(
                command,
                ["gh", "api", "repos/x/y/issues/comments/123"],
            )
            return _Completed(
                json.dumps(
                    {
                        "id": 123,
                        "html_url": "https://github.com/x/y/issues/1#issuecomment-123",
                        "body": (
                            "<!-- robert-comment task_id=task-production-canary "
                            "attempt_id=attempt-production-canary "
                            "event_fingerprints=production-canary:marker-suite -->\n"
                            "Robert production write canary."
                        ),
                    }
                )
            )

        result = acceptance_suite.acceptance_suite(
            self.config_path,
            workspace_dir=self.root / "suite",
            production_canary_evidence_url="https://github.com/x/y/issues/1#issuecomment-123",
            production_canary_marker_id="marker-suite",
            production_canary_run_command=fake_run,
            checks={
                "preflight": passing("preflight"),
                "live_discovery": passing("live_discovery"),
                "controlled_e2e": passing("controlled_e2e"),
                "live_worktree": passing("live_worktree"),
                "publish_dedupe": passing("publish_dedupe"),
                "live_worker": passing("live_worker"),
            },
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["next_actions"], [])
        self.assertEqual(result["production_write"]["status"], "verified")
        self.assertEqual(calls, [["gh", "api", "repos/x/y/issues/comments/123"]])


if __name__ == "__main__":
    unittest.main()

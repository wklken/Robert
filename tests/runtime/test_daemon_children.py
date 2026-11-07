import unittest
from pathlib import Path
from tests.support import PACKAGE_ROOT


class DaemonChildRunnerTests(unittest.TestCase):
    def test_daemon_resource_key_is_database_scoped(self):
        from robert_agent.runtime import daemon

        db_path = Path("/tmp/robert-state/../robert-state/robert.sqlite3")
        first = daemon._daemon_resource_key({"db_path": str(db_path), "repos": [{"full_name": "example/one"}]})
        second = daemon._daemon_resource_key({"db_path": str(db_path), "repos": [{"full_name": "example/two"}]})

        self.assertEqual(first, f"database:{db_path.resolve()}")
        self.assertEqual(second, first)

    def test_wait_for_next_decision_uses_listener_instead_of_sleep(self):
        from robert_agent.runtime import daemon

        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        class Listener:
            timeout = None

            def wait(self, timeout_seconds):
                self.timeout = timeout_seconds
                return True

        listener = Listener()

        notified = daemon._wait_for_next_decision(
            context,
            {"decision": "idle"},
            listener=listener,
            sleeper=lambda _seconds: self.fail("sleep must not run with a listener"),
            now="2026-07-03T00:00:00+00:00",
        )

        self.assertTrue(notified)
        self.assertEqual(listener.timeout, 30)

    def test_build_child_commands(self):
        from robert_agent.runtime import daemon

        run_once_cmd = daemon.build_run_once_command(
            "/tmp/config.yml",
            "/tmp/workflow.yml",
            dry_run=True,
        )
        loop_cmd = daemon.build_loop_command(
            "/tmp/config.yml",
            "/tmp/workflow.yml",
            dry_run=True,
            max_seconds=180,
        )

        self.assertIn(str(PACKAGE_ROOT / "runtime/cycle.py"), run_once_cmd)
        self.assertIn("--dry-run", run_once_cmd)
        self.assertIn(str(PACKAGE_ROOT / "runtime/loop.py"), loop_cmd)
        self.assertIn("--skip-external", loop_cmd)
        self.assertIn("--max-seconds", loop_cmd)
        self.assertIn("180", loop_cmd)
        self.assertNotIn("--skip-publish", loop_cmd)

        skip_publish_cmd = daemon.build_loop_command(
            "/tmp/config.yml",
            "/tmp/workflow.yml",
            dry_run=True,
            max_seconds=180,
            skip_publish=True,
        )
        self.assertIn("--skip-publish", skip_publish_cmd)

    def test_run_child_json_parses_single_json_object(self):
        from robert_agent.runtime import daemon

        class Completed:
            returncode = 0
            stdout = '{"ok": true, "status": "completed"}\n'
            stderr = ""

        def runner(command, **kwargs):
            return Completed()

        result = daemon.run_child_json(
            ["python3", "-c", "print('ok')"],
            timeout_seconds=5,
            runner=runner,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["child"]["returncode"], 0)

    def test_run_child_json_reports_non_json_and_nonzero(self):
        from robert_agent.runtime import daemon

        class Completed:
            returncode = 3
            stdout = "not json"
            stderr = "bad things"

        def runner(command, **kwargs):
            return Completed()

        result = daemon.run_child_json(
            ["python3", "bad.py"],
            timeout_seconds=5,
            runner=runner,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "child_failed")
        self.assertEqual(result["child"]["returncode"], 3)
        self.assertIn("bad things", result["safe_error"])

    def test_run_child_json_reports_timeout(self):
        import subprocess
        from robert_agent.runtime import daemon

        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        result = daemon.run_child_json(
            ["python3", "slow.py"],
            timeout_seconds=5,
            runner=runner,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["status"], "child_timeout")

    def test_rate_limit_guard_skips_low_search_or_core(self):
        from robert_agent.runtime import daemon

        config = {"min_search_remaining": 10, "min_core_remaining": 500}
        parsed = daemon.parse_rate_limit(
            {
                "resources": {
                    "core": {"remaining": 499, "reset": 1780000000},
                    "search": {"remaining": 30, "reset": 1780000100},
                }
            }
        )
        decision = daemon.should_skip_live_poll_for_rate_limit(parsed, config)

        self.assertTrue(decision["skip"])
        self.assertEqual(decision["reason"], "core_rate_limit_floor")

        parsed = daemon.parse_rate_limit(
            {
                "resources": {
                    "core": {"remaining": 5000, "reset": 1780000000},
                    "search": {"remaining": 9, "reset": 1780000100},
                }
            }
        )
        decision = daemon.should_skip_live_poll_for_rate_limit(parsed, config)

        self.assertTrue(decision["skip"])
        self.assertEqual(decision["reason"], "search_rate_limit_floor")

    def test_rate_limit_guard_allows_sufficient_budget(self):
        from robert_agent.runtime import daemon

        parsed = daemon.parse_rate_limit(
            {
                "resources": {
                    "core": {"remaining": 500, "reset": 1780000000},
                    "search": {"remaining": 10, "reset": 1780000100},
                }
            }
        )
        decision = daemon.should_skip_live_poll_for_rate_limit(
            parsed,
            {"min_search_remaining": 10, "min_core_remaining": 500},
        )

        self.assertFalse(decision["skip"])
        self.assertEqual(decision["reason"], "rate_limit_ok")

    def test_fetch_rate_limit_parses_gh_payload(self):
        from robert_agent.runtime import daemon

        class Completed:
            returncode = 0
            stdout = (
                '{"resources":{"core":{"remaining":1234,"reset":1780000000},'
                '"search":{"remaining":22,"reset":1780000100}}}'
            )
            stderr = ""

        def runner(command, **_kwargs):
            self.assertEqual(command, ["gh", "api", "rate_limit"])
            return Completed()

        result = daemon.fetch_rate_limit(runner=runner)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "rate_limit_loaded")
        self.assertEqual(result["rate_limit"]["core"]["remaining"], 1234)
        self.assertEqual(result["rate_limit"]["search"]["remaining"], 22)

    def test_idle_sleep_seconds_uses_nearest_live_poll_deadline(self):
        from robert_agent.runtime import daemon

        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        self.assertEqual(
            daemon._idle_sleep_seconds(
                context,
                now="2026-07-03T00:00:00+00:00",
            ),
            30,
        )

        context.next_live_poll_at = "2026-07-03T00:00:07+00:00"
        self.assertEqual(
            daemon._idle_sleep_seconds(
                context,
                now="2026-07-03T00:00:00+00:00",
            ),
            7,
        )

        self.assertEqual(
            daemon._idle_sleep_seconds(
                context,
                now="2026-07-03T00:00:10+00:00",
            ),
            0,
        )

    def test_sleep_seconds_after_failed_local_drain_uses_local_poll_interval(self):
        from robert_agent.runtime import daemon

        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        self.assertEqual(
            daemon._sleep_seconds_after_decision(
                context,
                {"decision": "local_drain", "ok": False},
                now="2026-07-03T00:00:00+00:00",
            ),
            30,
        )

    def test_sleep_seconds_after_no_progress_local_drain_uses_local_poll_interval(self):
        from robert_agent.runtime import daemon

        context = daemon.DaemonContext(
            config_path="/tmp/config.yml",
            workflow_path=None,
            db_path=Path("/tmp/dd.sqlite3"),
            repo_id="repo-1",
            max_concurrency=3,
            daemon_config={
                "local_poll_seconds": 30,
                "live_run_timeout_seconds": 300,
                "local_drain_timeout_seconds": 180,
            },
            daemon_run_id="daemon-run-1",
            lease_id="lease-1",
        )

        self.assertEqual(
            daemon._sleep_seconds_after_decision(
                context,
                {
                    "decision": "local_drain",
                    "ok": True,
                    "result": {"stop_reason": "no_progress"},
                },
                now="2026-07-03T00:00:00+00:00",
            ),
            30,
        )

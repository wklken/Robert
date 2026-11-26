"""Contracts for the full-suite CI runner, exercised with real Python children."""

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from tests.support import REPO_ROOT


class TestRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def module(self, name, source):
        (self.root / f"test_{name}.py").write_text(textwrap.dedent(source))

    def run_tests(self, *arguments, env=None):
        return subprocess.run(
            [sys.executable, "-B", "-m", "tests.run", "--start-directory", str(self.root), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_parallel_modules_execute_every_case_once_and_preserve_fixtures(self):
        for name, peer in (("one", "two"), ("two", "one")):
            self.module(
                name,
                f'''
                import os
                from pathlib import Path
                import time
                import unittest
                root = Path(__file__).parent
                def record(event):
                    with (root / "{name}.events").open("a") as stream:
                        stream.write(event + "\\n")
                def setUpModule():
                    record("module setup")
                def tearDownModule():
                    record("module cleanup")
                class Cases(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls):
                        record("class setup")
                    @classmethod
                    def tearDownClass(cls):
                        record("class cleanup")
                    def test_a_barrier(self):
                        (root / "{name}.ready").write_text(str(os.getpid()))
                        deadline = time.monotonic() + 10
                        while not (root / "{peer}.ready").exists():
                            self.assertLess(time.monotonic(), deadline, "modules were not run concurrently")
                            time.sleep(0.01)
                        record("a")
                    def test_b(self):
                        record("b")
                ''',
            )
        result = self.run_tests("--workers", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Completed 4 tests", result.stdout)
        for name in ("one", "two"):
            self.assertEqual(
                (self.root / f"{name}.events").read_text().splitlines(),
                ["module setup", "class setup", "a", "b", "class cleanup", "module cleanup"],
            )
        self.assertNotEqual((self.root / "one.ready").read_text(), (self.root / "two.ready").read_text())

    def test_failure_is_reported_without_skipping_other_modules(self):
        self.module(
            "bad",
            "import unittest\nclass Cases(unittest.TestCase):\n def test_bad(self): self.fail('sentinel failure')",
        )
        self.module(
            "good",
            """
            from pathlib import Path
            import unittest
            class Cases(unittest.TestCase):
                def test_good(self):
                    Path(__file__).with_suffix('.ran').touch()
            """,
        )
        result = self.run_tests("--workers", "1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("sentinel failure", result.stdout)
        self.assertIn("Completed 2 tests", result.stdout)
        self.assertTrue((self.root / "test_good.ran").is_file())

    def test_module_process_state_is_isolated_even_with_one_worker(self):
        self.module(
            "a_mutate",
            "import os, unittest\nclass Cases(unittest.TestCase):\n def test_mutate(self): os.environ['ROBERT_RUNNER_FIXTURE'] = 'changed'",
        )
        self.module(
            "b_check",
            "import os, unittest\nclass Cases(unittest.TestCase):\n def test_check(self): self.assertNotIn('ROBERT_RUNNER_FIXTURE', os.environ)",
        )
        result = self.run_tests("--workers", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ROBERT_RUNNER_FIXTURE", os.environ)

    def test_module_processes_cache_bytecode_outside_the_tested_tree(self):
        (self.root / "fixture_helper.py").write_text("VALUE = 1\n")
        self.module(
            "imports_helper",
            "import unittest, fixture_helper\nclass Cases(unittest.TestCase):\n def test_import(self): self.assertEqual(fixture_helper.VALUE, 1)",
        )
        temporary_home = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_home.cleanup)
        home = Path(temporary_home.name)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE"}
        }
        result = self.run_tests("--workers", "1", env={**environment, "HOME": str(home)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(self.root.rglob("__pycache__")), [])
        cached = [path.name for path in (home / ".cache" / "robert-test-pycache").rglob("*.pyc")]
        self.assertTrue(any(name.startswith("fixture_helper.") for name in cached), cached)

    def test_list_matches_standard_discovery_without_running_tests(self):
        self.module(
            "one", "import unittest\nclass Cases(unittest.TestCase):\n def test_one(self): self.fail('must not run')"
        )
        result = self.run_tests("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["test_one.Cases.test_one"])

    def test_import_error_fails_discovery(self):
        self.module("broken", "raise ImportError('sentinel import failure')")
        result = self.run_tests()
        self.assertEqual(result.returncode, 1)
        self.assertIn("sentinel import failure", result.stderr)
        self.assertNotIn("Completed", result.stdout)

    def test_module_level_skip_keeps_standard_discovery_semantics(self):
        self.module("platform", "import unittest\nraise unittest.SkipTest('platform unavailable')")
        result = self.run_tests()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipped=1", result.stdout)
        self.assertIn("Completed 1 tests", result.stdout)

    def test_interrupt_cancels_queued_modules(self):
        from concurrent.futures import Future
        from tests import run as runner

        for name in ("interrupt_one", "interrupt_two"):
            self.module(name, "import unittest\nclass Cases(unittest.TestCase):\n def test_case(self): pass")
        for interrupted_during in ("submission", "waiting"):
            with (
                self.subTest(interrupted_during=interrupted_during),
                mock.patch.dict(sys.modules),
                mock.patch.object(sys, "path", sys.path.copy()),
                mock.patch.object(runner, "ThreadPoolExecutor") as factory,
                mock.patch.object(runner, "as_completed", side_effect=KeyboardInterrupt),
            ):
                executor = factory.return_value.__enter__.return_value
                executor.submit.side_effect = (
                    [Future(), KeyboardInterrupt] if interrupted_during == "submission" else [Future(), Future()]
                )
                with self.assertRaises(KeyboardInterrupt):
                    runner.main(["--start-directory", str(self.root), "--workers", "1"])
                executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_custom_load_tests_cannot_silently_lose_instance_configuration(self):
        self.module(
            "custom",
            """
            import unittest
            class Cases(unittest.TestCase):
                def test_configured(self):
                    self.assertEqual(self.configured, 'from hook')
            def load_tests(loader, tests, pattern):
                case = Cases('test_configured')
                case.configured = 'from hook'
                return unittest.TestSuite([case])
            """,
        )
        result = self.run_tests()
        self.assertEqual(result.returncode, 1)
        self.assertIn("load_tests", result.stderr)
        self.assertIn("serial unittest", result.stderr)

    def test_empty_discovery_cannot_pass(self):
        result = self.run_tests()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No tests discovered", result.stderr)

    def test_worker_count_must_be_positive(self):
        result = self.run_tests("--workers", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--workers must be positive", result.stderr)

    def test_subtest_failure_propagates(self):
        self.module(
            "subtest",
            """
            import unittest
            class Cases(unittest.TestCase):
                def test_subtest(self):
                    for value in (1, 2):
                        with self.subTest(value=value):
                            self.assertEqual(value, 1)
            """,
        )
        result = self.run_tests()
        self.assertEqual(result.returncode, 1)
        self.assertIn("value=2", result.stdout)

    def test_json_report_records_every_module_and_tested_content(self):
        self.module("reported", "import unittest\nclass Cases(unittest.TestCase):\n def test_case(self): pass")
        report = self.root / "report.json"
        result = self.run_tests("--report-json", str(report))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(report.read_text())
        self.assertEqual(value["total_tests"], 1)
        self.assertEqual(value["failed_modules"], 0)
        self.assertTrue(value["content_unchanged"])
        self.assertEqual(len(value["source_sha256"]), 64)
        self.assertEqual(value["source_sha256"], value["final_source_sha256"])
        self.assertEqual(Path(value["environment"]["executable"]), Path(sys.executable).resolve())
        self.assertTrue(value["environment_unchanged"])
        self.assertEqual(value["modules"][0]["module"], "test_reported")
        self.assertEqual(value["modules"][0]["exit_code"], 0)
        self.assertGreater(value["modules"][0]["seconds"], 0)
        again = self.run_tests("--report-json", str(report))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads(report.read_text())["source_sha256"], value["source_sha256"])

    def test_failure_report_preserves_other_module_results(self):
        self.module(
            "report_bad",
            "import unittest\nclass Cases(unittest.TestCase):\n def test_case(self): self.fail('expected')",
        )
        self.module("report_good", "import unittest\nclass Cases(unittest.TestCase):\n def test_case(self): pass")
        report = self.root / "failed.json"
        result = self.run_tests("--report-json", str(report))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        value = json.loads(report.read_text())
        self.assertEqual(value["total_tests"], 2)
        self.assertEqual(value["failed_modules"], 1)
        self.assertEqual(
            {row["module"]: row["exit_code"] for row in value["modules"]}, {"test_report_bad": 1, "test_report_good": 0}
        )

    def test_duplicate_run_refuses_before_discovery_and_releases_after_exit(self):
        self.module(
            "held",
            """
            from pathlib import Path
            import time, unittest
            class Cases(unittest.TestCase):
                def test_case(self):
                    root = Path(__file__).parent
                    (root / 'started').touch()
                    deadline = time.monotonic() + 10
                    while not (root / 'release').exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
            """,
        )
        first = subprocess.Popen(
            [sys.executable, "-B", "-m", "tests.run", "--start-directory", str(self.root)],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(first.wait)
        self.addCleanup((self.root / "release").touch)
        import time

        deadline = time.monotonic() + 10
        while not (self.root / "started").exists():
            self.assertLess(time.monotonic(), deadline)
            self.assertIsNone(first.poll())
            time.sleep(0.01)
        duplicate = self.run_tests()
        (self.root / "release").touch()
        stdout, stderr = first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        self.assertEqual(duplicate.returncode, 2, duplicate.stdout + duplicate.stderr)
        self.assertIn("already running", duplicate.stderr)
        self.assertNotIn("Discovered", duplicate.stdout)
        self.assertEqual(self.run_tests().returncode, 0)

    def test_report_rejects_source_changes_during_run(self):
        from tests import run as runner
        from tests.support import test_run

        self.module("drift", "import unittest\nclass Cases(unittest.TestCase):\n def test_case(self): pass")
        report = self.root / "drift.json"
        with mock.patch.object(test_run, "source_digest", side_effect=["a" * 64, "b" * 64]):
            status = runner.main(["--start-directory", str(self.root), "--report-json", str(report)])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(report.read_text())["content_unchanged"])

    def test_report_rejects_real_test_file_changes(self):
        self.module(
            "modify",
            """
            from pathlib import Path
            import unittest
            class Cases(unittest.TestCase):
                def test_modify(self):
                    path = Path(__file__)
                    path.write_text(path.read_text() + '\\n# modified during verification\\n')
            """,
        )
        report = self.root / "report.json"
        result = self.run_tests("--report-json", str(report))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Source changed", result.stderr)
        self.assertFalse(json.loads(report.read_text())["content_unchanged"])

    def test_report_rejects_dependency_changes_during_run(self):
        from tests import run as runner
        from tests.support import test_run

        self.module("environment", "import unittest\nclass Cases(unittest.TestCase):\n def test_case(self): pass")
        report = self.root / "report.json"
        with (
            mock.patch.dict(sys.modules),
            mock.patch.object(sys, "path", sys.path.copy()),
            mock.patch.object(
                test_run, "environment_record", side_effect=[{"packages": {}}, {"packages": {"new": "1"}}]
            ),
        ):
            code = runner.main(["--start-directory", str(self.root), "--report-json", str(report)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(report.read_text())["environment_unchanged"])

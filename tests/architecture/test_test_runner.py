"""Contracts for the full-suite CI runner, exercised with real Python children."""

import os
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

    def run_tests(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", "-m", "tests.run", "--start-directory", str(self.root), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
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

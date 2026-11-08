"""Run every discovered unittest in isolated module processes using the selected Python."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


class ModuleLoader(unittest.TestLoader):
    def loadTestsFromModule(self, module, *, pattern=None):
        # Named replay cannot preserve hook-created instances or suite wrappers.
        if hasattr(module, "load_tests"):
            raise ValueError(f"Custom load_tests hooks require the serial unittest runner: {module.__name__}")
        return super().loadTestsFromModule(module, pattern=pattern)


def test_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def discover(start_directory):
    loader = ModuleLoader()
    suite = loader.discover(str(start_directory))
    if loader.errors:
        raise ValueError("Test discovery failed:\n" + "\n".join(loader.errors))
    modules = defaultdict(list)
    discovery_cases = []
    seen = set()
    for case in test_cases(suite):
        identifier = case.id()
        if identifier in seen:
            raise ValueError(f"Duplicate test ID: {identifier}")
        seen.add(identifier)
        # Module-level SkipTest produces a synthetic case which cannot be
        # imported by name in a child. Let unittest report it here instead.
        if type(case).__module__ == unittest.TestLoader.__module__:
            discovery_cases.append(case)
        else:
            modules[type(case).__module__].append(identifier)
    if not seen:
        raise ValueError("No tests discovered")
    return modules, discovery_cases


def run_module(module, identifiers, environment):
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", *identifiers],
        env=environment,
        capture_output=True,
        text=True,
    )
    return module, len(identifiers), result, time.monotonic() - started


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--start-directory", type=Path, default=Path("tests"))
    parser.add_argument(
        "--list", action="store_true", help="List the complete discovered test IDs without running them"
    )
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    try:
        modules, discovery_cases = discover(args.start_directory)
    except (ImportError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    if args.list:
        identifiers = [identifier for group in modules.values() for identifier in group]
        print("\n".join(sorted([*identifiers, *(case.id() for case in discovery_cases)])))
        return 0
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(args.start_directory.resolve()), environment.get("PYTHONPATH")))
    )
    total = sum(map(len, modules.values())) + len(discovery_cases)
    started = time.monotonic()
    print(f"Discovered {total} tests in {len(modules)} modules; workers={args.workers}", flush=True)
    failures = 0
    if discovery_cases:
        result = unittest.TextTestRunner(stream=sys.stdout).run(unittest.TestSuite(discovery_cases))
        failures += not result.wasSuccessful()
    # Larger modules start first to reduce the serial tail; fixtures stay in one process.
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        try:
            futures = [
                executor.submit(run_module, module, identifiers, environment)
                for module, identifiers in sorted(modules.items(), key=lambda item: (-len(item[1]), item[0]))
            ]
            for future in as_completed(futures):
                module, count, result, duration = future.result()
                failures += result.returncode != 0
                status = "PASS" if result.returncode == 0 else "FAIL"
                print(f"{status} {module}: {count} tests, {duration:.3f}s; exit={result.returncode}", flush=True)
                print(result.stdout + result.stderr, end="", flush=True)
        except KeyboardInterrupt:
            # Also cancel partially submitted batches before the context waits.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    print(f"Completed {total} tests in {time.monotonic() - started:.3f}s; failed modules={failures}", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())

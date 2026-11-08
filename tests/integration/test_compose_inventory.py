"""The Docker-free inventory keeps each expanded scenario independently runnable."""

from collections import Counter
from pathlib import Path
import subprocess
import sys
import unittest


class ComposeInventoryTests(unittest.TestCase):
    def test_public_list_has_distinct_expanded_cases_without_docker(self):
        runner = Path(__file__).resolve().parents[1] / "simulations" / "compose" / "run.py"
        output = subprocess.check_output(
            [sys.executable, "-B", str(runner), "--compose", "missing-compose-executable", "--list"],
            text=True,
            timeout=10,
        )
        rows = [line.split("\t") for line in output.splitlines()]
        self.assertEqual(len(rows), len({case for case, _, _ in rows}))
        self.assertEqual(Counter(suite for _, suite, _ in rows), {"core": 46, "extended": 75, "slow": 5})

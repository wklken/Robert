"""Scripted review repair and independent final verification outputs."""

import json
from pathlib import Path
import subprocess
import sys


FINDING = {
    "id": "F1",
    "title": "Missing coverage for negative operands",
    "severity": "medium",
    "location": {"path": "calc.py", "line": 2},
    "trigger": "Adding a negative number",
    "impact": "The changed arithmetic can regress without a test failure.",
    "evidence": "The candidate checks only positive inputs.",
    "suggestion": "Add a test that adds a negative operand.",
}


def address(facts):
    test = Path("test_calc.py")
    test.write_text(
        "from unittest import TestCase\n"
        "from calc import add\n\n"
        "class AdditionTest(TestCase):\n"
        "    def test_negative_operand(self):\n"
        "        self.assertEqual(add(-2, 3), 1)\n"
    )
    subprocess.run([sys.executable, "-B", "-m", "unittest", "test_calc"], timeout=10, check=True)
    subprocess.run(["git", "add", str(test)], timeout=10, check=True)
    subprocess.run(["git", "commit", "-m", "Cover addition with a negative operand"], timeout=10, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10, text=True).strip()
    return {
        "candidate_sha": facts["candidate_sha"],
        "final_sha": sha,
        "report_sha256": facts["report_sha256"],
        "dispositions": [{"id": FINDING["id"], "status": "fixed", "reason": "Added and ran a negative operand test."}],
        "verification": [
            {
                "command": [sys.executable, "-B", "-m", "unittest", "test_calc"],
                "status": "passed",
                "purpose": "Cover addition with a negative operand",
                "required": True,
                "exit_code": 0,
                "source": "worker_reported",
            }
        ],
        "body": "Use addition and cover negative operands.\n\n## Assumptions\n\nNone.",
    }


def verify(facts, input_path):
    inputs = json.loads(Path(input_path).read_text())
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10, text=True).strip()
    if sha != facts["final_sha"] or sha != inputs["final_sha"]:
        raise ValueError("final verification was given the wrong revision")
    if "test_negative_operand" not in Path(inputs["additional_diff_path"]).read_text():
        raise ValueError("review handoff omitted the repair diff")
    subprocess.run([sys.executable, "-B", "-m", "unittest", "test_calc"], timeout=10, check=True)
    keys = (
        "round_id",
        "snapshot_id",
        "candidate_sha",
        "final_sha",
        "report_sha256",
        "dispositions_sha256",
        "additional_diff_sha256",
    )
    return {
        **{key: facts[key] for key in keys},
        "schema_version": 1,
        "recommendation": "approved",
        "checked_findings": [
            {"id": finding, "status": "resolved", "reason": "Confirmed the added test in the exact final commit."}
            for finding in inputs["finding_ids"]
        ],
        "blocking_findings": [],
    }

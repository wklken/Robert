"""Scripted agent process using Robert's public Worker protocol."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from git_transport import activity


def prompt_identity(prompt):
    prefix = "# Robert agent prompt\n\n## Stage\n\n"
    if not prompt.startswith(prefix):
        raise ValueError("unexpected worker prompt contract")
    stage, end, _ = prompt[len(prefix) :].partition("\n## Task\n")
    if not end:
        raise ValueError("missing Task section")
    fields = {}
    for line in stage.splitlines():
        key, separator, value = line.partition(": ")
        if separator:
            if key in fields:
                raise ValueError(f"duplicate Stage field: {key}")
            fields[key] = value
    for key in ("task_id", "attempt_id", "workstream_id", "origin_type", "expected_output"):
        if not fields.get(key):
            raise ValueError(f"missing worker Stage field: {key}")
    return fields


def binding(root, identity):
    return [
        "--db",
        str(root / "data" / "robert.sqlite3"),
        "--task-id",
        identity["task_id"],
        "--attempt-id",
        identity["attempt_id"],
    ]


def run_check(root, identity):
    check = [sys.executable, "-B", "-c", "from calc import add; assert add(2, 3) == 5"]
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "robert_agent.worker.heartbeat",
            *binding(root, identity),
            "--phase",
            "verify",
            "--cwd",
            str(Path.cwd()),
            "--",
            *check,
        ],
        check=True,
        timeout=20,
    )
    return {
        "command": "python -B -c 'from calc import add; assert add(2, 3) == 5'",
        "status": "passed",
        "purpose": "Verify the fixed addition",
        "required": True,
        "source": "worker_reported",
        "exit_code": 0,
    }


def result(root, identity, policy):
    expected = identity["expected_output"]
    web = identity["origin_type"] == "web"
    origin_key = "work_item_event_ids" if web else "event_fingerprints"
    envelope = {
        "planned_github_actions": [],
        "used_skills": [],
        "verification": [],
        "handoff": "Scripted fixture work completed.",
        f"consumed_{origin_key}": json.loads(identity[origin_key]),
    }
    actions = envelope["planned_github_actions"]
    comment_body = policy.get("comment", "The fixture addition uses subtraction.")
    comment = {"type": "comment", "body": comment_body}
    if expected == "classification_result":
        route = policy.get("classify", "local-result" if web else "comment-analysis")
        envelope.update(recommended_route=route, review_input_kind="request")
        if route == "new-pr":
            envelope["branch_slug"] = "fix-fixture-addition"
    elif expected in {"new_pr", "update_existing_pr"}:
        if identity["task_kind"] == "merge_conflict_remediation":
            merge = subprocess.run(
                ["git", "merge", "--no-ff", "--no-commit", identity["observed_base_sha"]],
                timeout=15,
            )
            if merge.returncode not in {0, 1}:
                raise RuntimeError(f"fixture merge exited {merge.returncode}")
            subprocess.run(["git", "rev-parse", "--verify", "MERGE_HEAD"], check=True, timeout=10)
        source = Path("calc.py")
        if policy.get("dirty_hold") and identity["Mode"] == "resume" and "return x + y" not in source.read_text():
            raise RuntimeError("recovery lost the first attempt's dirty work")
        source.write_text("def add(x, y):\n    return x + y\n")
        if expected == "update_existing_pr" and policy.get("followup"):
            with Path("calc.py").open("a") as source:
                source.write("\n# Preserve the public addition API.\n")
        if identity["task_kind"] == "ci_remediation":
            Path("test_calc.py").write_text(
                "import unittest\nfrom calc import add\n\n"
                "class AdditionTest(unittest.TestCase):\n"
                "    def test_addition(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n"
            )
            subprocess.run([sys.executable, "-B", "-m", "unittest", "test_calc"], check=True, timeout=10)
            subprocess.run(["git", "add", "test_calc.py"], check=True, timeout=10)
        envelope["verification"].append(run_check(root, identity))
        if policy.get("dirty_hold"):
            decision = activity("worker", identity, "dirty_ready")
            if decision != "continue":
                raise RuntimeError("dirty worker aborted")
        subprocess.run(["git", "add", "calc.py"], check=True, timeout=10)
        subprocess.run(["git", "commit", "-m", "Fix fixture addition"], check=True, timeout=10)
        actions.append({"type": "push_existing_pr"})
        if expected == "new_pr":
            actions.append(
                {
                    "type": "open_pr",
                    "title": "Fix fixture addition",
                    "issue_resolution": "complete",
                    "body": "## Summary\n\nUse addition in calc.py.\n\n## Assumptions\n\nNone.",
                }
            )
        else:
            actions.append(comment)
        if identity["task_kind"] in {"ci_remediation", "merge_conflict_remediation"}:
            envelope["remediation_evidence"] = {"resolution_summary": "Corrected the fixture addition."}
            skill = "fast-test-fix" if identity["task_kind"] == "ci_remediation" else "fast-conflict-fix"
            for name in ("fast-preflight", skill):
                content = (root / "skills" / name / "SKILL.md").read_text()
                if not content.strip():
                    raise ValueError(f"empty required fixture skill: {name}")
                envelope["used_skills"].append(name)
    elif expected == "waiting_for_user" or policy.get("wait") and identity["Mode"] == "fresh":
        expected = "waiting_for_user"
        envelope["operator_question"] = {
            "kind": "clarification",
            "summary": "Should the fixture preserve the existing API?",
            "choices": [
                {"id": "preserve", "label": "Preserve the API"},
                {"id": "change", "label": "Change the API"},
            ],
        }
        if not web:
            actions.append({"type": "comment", "body": envelope["operator_question"]["summary"]})
    elif expected in {"comment_analysis", "review_comment", "pr_review_comment"}:
        actions.append(comment)
    elif expected != "local_result":
        raise ValueError(f"unsupported fixture output: {expected}")
    mutation = policy.get("invalid")
    if mutation == "action" and expected == "comment_analysis":
        actions[:] = [{"type": "open_pr", "title": "Forbidden", "body": "Forbidden"}]
    elif mutation == "target" and expected == "comment_analysis":
        actions[0]["target_url"] = "https://github.com/example/other/issues/99"
    elif mutation == "verification" and expected == "new_pr":
        envelope["verification"][0].update(status="failed", exit_code=1)
    elif mutation == "dirty" and expected == "new_pr":
        Path("calc.py").write_text("raise RuntimeError('dirty')\n")
    return expected, envelope


def submit(root, identity, expected, envelope, *, invalid_json=False):
    argv = [
        sys.executable,
        "-B",
        "-m",
        "robert_agent.worker.result",
        *binding(root, identity),
        "--output-type",
        expected,
    ]
    return json.loads(
        subprocess.check_output(argv, input="{" if invalid_json else json.dumps(envelope), text=True, timeout=20)
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--name", default="fixture")
    options = parser.parse_args()
    root = options.root
    prompt = sys.stdin.read()
    identity = prompt_identity(prompt)
    policy = json.loads((root / "worker-policy.json").read_text())
    directory = root / "actors" / identity["attempt_id"]
    directory.mkdir(parents=True)
    (directory / "prompt.md").write_text(prompt)
    actor = {key: identity[key] for key in ("task_id", "attempt_id", "workstream_id", "expected_output")}
    actor["worker"] = options.name
    actor["pid"] = os.getpid()
    actor["pgid"] = os.getpgrp()
    if policy.get("descendant") and identity["expected_output"] != "classification_result":
        ready = directory / "descendant-ready"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(330)",
                str(ready),
            ]
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            raise RuntimeError("descendant failed to start")
        actor["descendant_pid"] = child.pid
    if activity("worker", actor, "ready") != "continue":
        return 1
    if policy.get("heartbeat_hold") and identity["expected_output"] != "classification_result":
        # Exercise the actual heartbeat writer while a command remains live.
        heartbeat = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "robert_agent.worker.heartbeat",
                *binding(root, identity),
                "--phase",
                "analyze",
                "--heartbeat-interval-seconds",
                "2",
                "--",
                sys.executable,
                "-c",
                "import time; time.sleep(40)",
            ]
        )
        try:
            activity("worker", actor, "heartbeat_ready")
        finally:
            heartbeat.terminate()
            heartbeat.wait(timeout=5)
        return 0
    if policy.get("exit_without_result") and identity["expected_output"] != "classification_result":
        return 0
    expected, envelope = result(root, identity, policy)
    (directory / "submission.json").write_text(json.dumps({"output_type": expected, "submission": envelope}))
    if activity("worker", actor, "before_submit") != "continue":
        return 1
    received = submit(root, identity, expected, envelope, invalid_json=policy.get("invalid") == "json")
    (directory / "receipt.json").write_text(json.dumps(received))
    print(json.dumps(received))
    if activity("worker", {**actor, "status": received["status"]}, "after_submit") != "continue":
        return 1
    return 0 if received.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

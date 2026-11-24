"""Pre-publication review blocks Git effects until exact final verification."""

import json
from pathlib import Path
import sys

from bootstrap import HARNESS, REPO, STATE, run
from scenarios_delivery import completed, effect_rows, setup


def verify_final(w):
    setup(w, coding=True, pre_pr_finding=True)

    def configure(cfg):
        for worker in ("fixture", "reviewer"):
            cfg["workers"][worker] = {
                **cfg["workers"]["fixture"],
                "command": [sys.executable, str(HARNESS / "fake_models.py"), str(STATE), worker],
                "timeout_seconds": 65,
            }
        cfg["repos"][0]["routes"] = {"new-pr": {"pre_pr_review": {"workers": ["reviewer"]}}}

    w.config(configure)
    hold = w.fault("model", {"stage": "review.verify_final", "worker": "reviewer"}, "output_ready", seconds=120)
    w.start()
    point = w.checkpoint(hold, timeout=105)
    assert not w.actions() and not w.remote()["effects"]
    bare = f"--git-dir=/git/{REPO}.git"
    assert run("git", bare, "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"
    path = Path(point["entity"]["run_directory"]) / "final-verification.json"
    proof = json.loads(path.read_text())
    assert proof["final_sha"] != proof["candidate_sha"]
    assert proof["checked_findings"][0]["id"] == "F1"
    w.release(point)
    (task,) = completed(w, "new-pr")
    (pr,) = effect_rows(w, "pr_create")
    assert pr["effect"]["head"]["sha"] == proof["final_sha"]
    assert run("git", bare, "rev-parse", pr["effect"]["head"]["ref"]) == proof["final_sha"]
    assert "test_negative_operand" in run("git", bare, "show", f"{proof['final_sha']}:test_calc.py")
    assert "Missing coverage for negative operands" in pr["effect"]["body"]
    assert [row["action_type"] for row in w.actions(status="published") if row["task_id"] == task["task_id"]] == [
        "push_existing_pr",
        "open_pr",
    ]

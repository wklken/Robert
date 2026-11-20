"""Worker receipt, process lifetime, and durable result ingestion."""

from contextlib import closing, nullcontext
from pathlib import Path
import sqlite3
import time

from bootstrap import DB, STATE
from observation import process
from scenarios_delivery import completed, effect_rows, published, setup


def hold_receipt(w):
    setup(w)
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "after_submit")
    w.start()
    cp = w.checkpoint(hold)
    attempt = cp["entity"]["attempt_id"]
    assert cp["entity"]["status"] == "recorded"
    status, created = w.post(
        "/api/work-items",
        {
            "repo_id": "repo:example/other",
            "title": "Independent queued work",
            "description": "Analyze fixture behavior.",
            "mode": "start",
        },
    )
    assert status == 201, created
    assert len(w.sql("SELECT * FROM attempts")) == 1
    for _ in range(6):
        assert len(w.sql("SELECT * FROM attempts")) == 1
        assert process(cp["entity"]["pid"])
        assert len(w.sql("SELECT * FROM worker_results WHERE attempt_id=?", attempt)) == 1
        assert not w.actions(status="published") and not w.remote()["effects"]
        time.sleep(1)
    assert len(w.sql("SELECT * FROM attempts")) == 1
    w.release(cp)
    completed(w, "comment-analysis")
    completed(w, "local-result")
    assert len(effect_rows(w, "comment")) == 1
    assert len(w.sql("SELECT * FROM worker_results WHERE output_type='local_result'")) == 1


def deferred(w, *, restart=False):
    setup(w)
    before = w.fault("worker", {"expected_output": "comment_analysis"}, "before_submit")
    w.start()
    cp = w.checkpoint(before)
    w.registered_worker(cp)
    attempt = cp["entity"]["attempt_id"]
    after = w.fault("worker", {"attempt_id": attempt}, "after_submit")
    with w.suspend_daemon() if restart else nullcontext() as daemon, closing(sqlite3.connect(DB, timeout=5)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        w.note("db_lock_acquired", attempt_id=attempt)
        w.release(cp)
        receipt = w.checkpoint(after, timeout=20)
        assert receipt["entity"]["status"] == "deferred"
        spool = list(Path(str(DB) + ".results").glob("*.json"))
        assert len(spool) == 1 and not w.sql("SELECT * FROM worker_results WHERE attempt_id=?", attempt)
        if restart:
            from scenarios_runtime import daemon_lease, restart_expired

            w.release(receipt)
            from observation import group

            w.wait("deferred Worker exited", lambda: not group(receipt["entity"]["pgid"]))
            lease = daemon_lease(w)
            w.kill(daemon, whole_group=False)
        lock.rollback()
        w.note("db_lock_released", attempt_id=attempt)
    if restart:
        restart_expired(w, lease)
    else:
        w.release(receipt)
    completed(w, "comment-analysis")
    assert len(published(w)) == len(effect_rows(w, "comment")) == 1
    assert not spool[0].exists()
    assert (STATE / "actors" / attempt / "receipt.json").is_file()


def no_result(w):
    setup(w, exit_without_result=True)
    rule = w.fault("worker", {"expected_output": "comment_analysis"}, "ready")
    w.start()
    cp = w.checkpoint(rule)
    w.wait(
        "worker identity durably registered",
        lambda: w.sql(
            "SELECT attempt_id FROM attempts WHERE attempt_id=? AND json_extract(metadata_json,'$.dispatch.process_start_token') IS NOT NULL",
            cp["entity"]["attempt_id"],
        ),
    )
    w.release(cp)
    w.wait(
        "failed task after a normal process exit",
        lambda: w.sql("SELECT task_id FROM tasks WHERE route_id='comment-analysis' AND lifecycle='failed'"),
    )
    assert not w.remote()["effects"]


def replay(w, actor, document=None):
    import json
    import subprocess
    import sys

    saved = STATE / "actors" / actor["attempt_id"] / "submission.json"
    document = document or json.loads(saved.read_text())
    result = subprocess.check_output(
        [
            sys.executable,
            "-B",
            "-m",
            "robert_agent.worker.result",
            "--db",
            str(DB),
            "--task-id",
            actor["task_id"],
            "--attempt-id",
            actor["attempt_id"],
            "--output-type",
            document["output_type"],
        ],
        input=json.dumps(document["submission"]),
        text=True,
        timeout=15,
    )
    return json.loads(result)


def cancel(w, *, descendant=False):
    from observation import group

    setup(w, descendant=descendant)
    rule = w.fault("worker", {"expected_output": "comment_analysis"}, "before_submit")
    w.start()
    cp = w.checkpoint(rule)
    actor = cp["entity"]
    assert group(actor["pgid"])
    item = w.wait("workbench item for this GitHub workstream", lambda: w.item(actor["workstream_id"]))

    status, result = w.cancel_item(item)
    assert status == 200, result
    w.wait("canceled worker stopped", lambda: not group(actor["pgid"]))
    assert w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='canceled'", actor["task_id"])
    assert not replay(w, actor)["ok"]
    w.release(cp, "abort")
    assert not w.remote()["effects"]


def unknown_descendant(w):
    from observation import group

    setup(w, descendant=True)
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "after_submit")
    w.start()
    cp = w.checkpoint(hold)
    actor = cp["entity"]
    assert cp["entity"]["status"] == "recorded"
    w.release(cp)
    w.wait(
        "leader stopped while its descendant is still alive",
        lambda: all(row["pid"] != actor["pid"] for row in group(actor["pgid"])),
    )
    w.wait(
        "unknown group enters execution attention",
        lambda: w.sql(
            "SELECT * FROM execution_subjects WHERE subject_type='task' AND subject_id=? AND binding_status='attention'",
            actor["task_id"],
        ),
    )
    assert any(row["pid"] == actor["descendant_pid"] for row in group(actor["pgid"]))
    assert not w.actions(status="published") and not w.remote()["effects"]


def recover_dirty(w, *, exhaust=False, reject_stale=False):
    import json

    setup(w, coding=True, dirty_hold=True)
    hold = w.fault("worker", {"expected_output": "new_pr"}, "dirty_ready")
    w.start()
    first = w.checkpoint(hold)
    actor = first["entity"]
    row = w.sql("SELECT worktree_path FROM attempts WHERE attempt_id=?", actor["attempt_id"])[0]
    original = Path(row["worktree_path"]) / "calc.py"
    assert "return x + y" in original.read_text()
    resumed = w.fault("worker", {"task_id": actor["task_id"], "expected_output": "new_pr"}, "dirty_ready")
    w.kill(process(first["actor"]["pgid"]), whole_group=True)
    w.release(first, "abort")
    second = w.checkpoint(resumed)
    current = second["entity"]
    assert current["attempt_id"] != actor["attempt_id"]
    attempts = w.sql("SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no", actor["task_id"])
    assert len(attempts) == 2
    assert w.sql(
        "SELECT recovery_attempts FROM execution_subjects WHERE subject_type='task' AND subject_id=?", actor["task_id"]
    ) == [{"recovery_attempts": 1}]
    if reject_stale:
        stale = {
            "output_type": "new_pr",
            "submission": {
                "planned_github_actions": [
                    {"type": "push_existing_pr"},
                    {"type": "open_pr", "title": "Old", "body": "Old", "issue_resolution": "complete"},
                ],
                "used_skills": [],
                "verification": [],
                "handoff": "Old Worker replay",
                "consumed_event_fingerprints": json.loads(actor["event_fingerprints"]),
            },
        }
        assert not replay(w, actor, stale)["ok"]
        assert not w.sql("SELECT * FROM worker_results WHERE attempt_id=?", actor["attempt_id"])
    if exhaust:
        w.program("restart", "daemon")
        assert w.sql(
            "SELECT recovery_attempts FROM execution_subjects WHERE subject_type='task' AND subject_id=?",
            actor["task_id"],
        ) == [{"recovery_attempts": 1}]
        w.kill(process(second["actor"]["pgid"]), whole_group=True)
        w.release(second, "abort")
        w.wait(
            "recovery exhausted",
            lambda: w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='failed'", actor["task_id"]),
        )
        assert len(w.sql("SELECT * FROM attempts WHERE task_id=?", actor["task_id"])) == 2
        assert not w.remote()["effects"]
    else:
        w.release(second)
        completed(w, "new-pr")
        assert len(effect_rows(w, "pr_create")) == 1


def bad_json(w):
    from scenarios_delivery import rejected

    rejected(w, "json")


def same_result(w):
    import json

    setup(w)
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "after_submit")
    w.start()
    cp = w.checkpoint(hold)
    actor = cp["entity"]
    original = json.loads((STATE / "actors" / actor["attempt_id"] / "receipt.json").read_text())
    repeated = replay(w, actor)
    assert repeated["ok"] and repeated["result_id"] == original["result_id"] and repeated["deduplicated"]
    document = json.loads((STATE / "actors" / actor["attempt_id"] / "submission.json").read_text())
    document["submission"]["planned_github_actions"][0]["body"] = "Different second submission."
    denied = replay(w, actor, document)
    assert denied["status"] == "result_conflict", denied
    w.release(cp)
    completed(w, "comment-analysis")
    assert len(effect_rows(w, "comment")) == 1


def cannot_exec(w):
    import sys
    from bootstrap import HARNESS

    setup(w)
    launcher = STATE / "non-executable-worker"
    launcher.write_text(f"#!{sys.executable}\n")
    launcher.chmod(0o644)
    w.config(lambda cfg: cfg["workers"]["fixture"].update(command=[str(launcher), str(HARNESS)]))
    w.start()
    w.wait(
        "launch failure persisted",
        lambda: w.sql("SELECT * FROM attempts WHERE status='failed' AND failure_json IS NOT NULL"),
    )
    assert not w.sql("SELECT * FROM attempts WHERE status='running'") and not w.remote()["effects"]


def worker_timeout(w, *, heartbeat=False):
    setup(w, heartbeat_hold=heartbeat)
    w.config(
        lambda cfg: cfg["supervision"].update(
            startup_grace_seconds=3, heartbeat_stale_seconds=6, hard_timeout_seconds=20, stall_seconds=4
        )
    )
    phase = "heartbeat_ready" if heartbeat else "ready"
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, phase)
    w.start()
    cp = w.checkpoint(hold)
    actor = cp["entity"]
    w.wait(
        "real process hard timeout",
        lambda: w.sql("SELECT * FROM attempts WHERE attempt_id=? AND status='failed'", actor["attempt_id"]),
        timeout=40,
    )
    from observation import group

    assert not group(actor["pgid"])
    assert not w.remote()["effects"]


def corrupt_spool(w):
    import os
    import signal
    from hashlib import sha256
    from bootstrap import OTHER
    from client import fixture
    from seeds import issue, thread, world

    seed = world(issue_data=issue())
    seed["repositories"][OTHER]["issues"]["12"] = issue(12, repo=OTHER)
    seed["notifications"].append(thread(OTHER, 12))
    w.seed(seed)
    w.policy()
    w.config(lambda cfg: cfg["scheduler"].update(max_concurrency=2))
    before = w.fault("worker", {"expected_output": "comment_analysis"}, "before_submit", hits=2)
    after = w.fault("worker", {"status": "deferred"}, "after_submit", hits=2)
    w.start(observer=False)

    def checkpoints(rule):
        return [cp for cp in fixture("/checkpoints?after_seq=0")["checkpoints"] if cp["rule_id"] == rule]

    waiting = w.wait(
        "two distinct active attempts", lambda: checkpoints(before) if len(checkpoints(before)) == 2 else None
    )
    for cp in waiting:
        w.registered_worker(cp)
    daemon_pid = w.pid("daemon")
    original = process(daemon_pid)
    os.kill(daemon_pid, signal.SIGSTOP)
    try:
        with closing(sqlite3.connect(DB, timeout=5)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            for cp in waiting:
                w.release(cp)
            saved = w.wait(
                "both submissions deferred", lambda: checkpoints(after) if len(checkpoints(after)) == 2 else None
            )
            bad, good = [cp["entity"]["attempt_id"] for cp in saved]
            path = Path(str(DB) + ".results") / (sha256(bad.encode()).hexdigest() + ".json")
            assert path.is_file()
            path.write_text("{", encoding="utf-8")
            lock.rollback()
            for cp in saved:
                w.release(cp)
    finally:
        if (current := process(daemon_pid)) and current["start_token"] == original["start_token"]:
            os.kill(daemon_pid, signal.SIGCONT)
    w.wait(
        "valid spool records and damaged spool is isolated",
        lambda: (
            w.sql("SELECT * FROM worker_results WHERE attempt_id=?", good) and path.with_suffix(".invalid").is_file()
        ),
    )
    published(w)
    assert not w.sql("SELECT * FROM worker_results WHERE attempt_id=?", bad)
    assert len(effect_rows(w, "comment")) == 1

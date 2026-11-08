"""Independent observer and recovery through real consumer entry points."""

from contextlib import closing
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from bootstrap import DB, OTHER, REPO, STATE
from observation import group, process
from scenarios_delivery import completed, setup
from seeds import world


def observing(w):
    setup(w)
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "ready")
    w.start()
    cp = w.checkpoint(hold)
    pid = w.pid("observer")
    prior = process(pid)
    w.kill(process(w.pid("daemon")), whole_group=False)
    w.wait(
        "observer reports an incident without daemon",
        lambda: w.sql("SELECT * FROM supervision_incidents WHERE status='open'"),
        timeout=60,
    )
    assert process(pid)["start_token"] == prior["start_token"]
    assert not w.sql("SELECT * FROM supervision_recovery_actions")
    w.release(cp)


def spool_recovery(w, *, authorized=True):
    setup(w)
    w.config(
        lambda cfg: cfg["supervision"].update(
            mode="recover_known",
            recovery={
                "allowed_actions": ["spool_replay"],
                "repositories": [REPO if authorized else OTHER],
            },
        )
    )
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "before_submit")
    w.start()
    cp = w.checkpoint(hold)
    w.registered_worker(cp)
    attempt = cp["entity"]["attempt_id"]
    defer = w.fault("worker", {"attempt_id": attempt}, "after_submit")
    key = sha256(attempt.encode()).hexdigest()
    spool = Path(str(DB) + ".results") / f"{key}.json"
    lock = spool.with_suffix(".lock")
    spool.parent.mkdir(exist_ok=True)
    with lock.open("a") as locked:
        with w.suspend_daemon(), closing(sqlite3.connect(DB, timeout=5)) as sqlite:
            sqlite.execute("BEGIN IMMEDIATE")
            w.release(cp)
            submitted = w.checkpoint(defer)
            assert submitted["entity"]["status"] == "deferred"
            saved = json.loads(spool.read_text())
            fcntl.flock(locked.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            sqlite.rollback()
            w.release(submitted)
        w.wait("worker stopped", lambda: not group(cp["entity"]["pgid"]))
        if authorized:
            requests = w.wait(
                "matching recovery requested",
                lambda: w.sql("SELECT * FROM supervision_recovery_actions WHERE action_kind='spool_replay'"),
            )
            assert len(requests) == 1
            action = requests[0]["action_id"]
        else:
            w.wait(
                "deferred result observed without recovery grant",
                lambda: w.sql("SELECT * FROM supervision_incidents WHERE rule_id='result_not_consumed'"),
            )
            assert not w.sql("SELECT * FROM supervision_recovery_actions")
        assert spool.is_file()
    completed(w, "comment-analysis")
    result = w.sql("SELECT result_id FROM worker_results WHERE attempt_id=?", attempt)
    assert len(result) == 1
    if authorized:
        verified = w.wait(
            "spool replay positively verified",
            lambda: w.sql(
                "SELECT * FROM supervision_recovery_actions WHERE action_id=? AND status='succeeded'", action
            ),
            timeout=75,
        )
        receipt = json.loads(verified[0]["receipt_json"])
        assert receipt == {
            "attempt_id": attempt,
            "result_id": result[0]["result_id"],
            "fingerprint": saved["fingerprint"],
        }
    else:
        assert not w.sql("SELECT * FROM supervision_recovery_actions")


def outage(w):
    w.seed(world())
    w.policy()
    w.start()
    health = DB.parent / "supervision" / sha256(str(DB).encode()).hexdigest()[:16] / "health.json"

    def current():
        return json.loads(health.read_text())

    w.wait("initial positive observation", lambda: current().get("ok"))
    w.program("stop", "daemon")
    with closing(sqlite3.connect(DB, timeout=5)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        w.wait("observer reports consecutive DB failures", lambda: current().get("consecutive_failures", 0) >= 2)
        assert current()["status"] == "unobservable"
        lock.rollback()
    w.wait("positive observation after lock released", lambda: current().get("ok"))
    assert not current().get("consecutive_failures")


def decision_config(w, action):
    import sys
    from bootstrap import HARNESS, write_json

    w.config(
        lambda cfg: cfg["supervision"].update(
            mode="decide",
            decision={
                "command": [sys.executable, str(HARNESS / "fake_decision.py"), str(STATE)],
                "timeout_seconds": 20,
                "cooldown_seconds": 1,
                "max_calls_per_hour": 60,
            },
        )
    )
    write_json(STATE / "decision-policy.json", {"action": action})


def decision_state(w, root_id):
    result = w.sql(
        "SELECT metadata_json FROM supervision_checkpoints WHERE scope_type='decision' AND scope_id=? AND rule_id='current'",
        root_id,
    )
    return json.loads(result[0]["metadata_json"]) if result else {}


def decision_setup(w, action):
    w.seed(world())
    w.policy()
    decision_config(w, action)
    w.start()
    hold = w.fault("worker", {"expected_output": "local_result"}, "ready")
    status, result = w.post(
        "/api/work-items",
        {
            "repo_id": "repo:example/repo",
            "title": "Decide fixture behavior",
            "description": "Analyze the fixture public API.",
            "mode": "start",
        },
    )
    assert status == 201, result
    cp = w.checkpoint(hold)
    return cp, cp["entity"]["workstream_id"]


def runner_state(w, status):
    return w.sql(
        "SELECT metadata_json FROM supervision_checkpoints WHERE scope_type='decision_runner' AND metadata_json LIKE ?",
        f'%"{status}"%',
    )


def invalid_candidate(w):
    cp, root = decision_setup(w, "invent")
    w.wait("invented candidate rejected", lambda: runner_state(w, "invalid_output"))
    assert not decision_state(w, root).get("candidate")
    assert len(w.sql("SELECT * FROM attempts")) == 1
    w.release(cp)


def ask_user(w):
    import subprocess
    from bootstrap import CONFIG

    cp, root = decision_setup(w, "ask_user")

    def pending_question():
        state = decision_state(w, root)
        return state["decision"]["bundle_id"] if state.get("decision", {}).get("action") == "ask_user" else None

    question = w.wait("durable model question", pending_question)
    command = [
        "robert",
        "supervisor",
        "reply",
        "--config",
        str(CONFIG),
        "--workstream-id",
        root,
        "--body",
        "Keep the API.",
    ]
    bad = subprocess.run([*command, "--decision-id", "obsolete"], capture_output=True, text=True, timeout=10)
    assert bad.returncode != 0
    answer = subprocess.run([*command, "--decision-id", question], capture_output=True, text=True, timeout=10)
    assert answer.returncode == 0, answer.stderr + answer.stdout
    assert len(w.sql("SELECT * FROM attempts")) == 1
    w.release(cp)


def notify(w):
    import time

    cp, root = decision_setup(w, "notify_user")
    w.wait("decision notice", lambda: decision_state(w, root).get("decision", {}).get("action") == "notify_user")
    before = w.sql("SELECT notification_id FROM notifications ORDER BY notification_id")
    assert before
    time.sleep(5)
    assert w.sql("SELECT notification_id FROM notifications ORDER BY notification_id") == before
    assert not w.remote()["effects"]
    w.release(cp)


def finish(w, *, completed_task=False):
    from bootstrap import write_json

    cp, root = decision_setup(w, "wait" if completed_task else "finish_supervision")
    if completed_task:
        write_json(STATE / "decision-policy.json", {"action": "finish_supervision"})
        w.release(cp)
        completed(w, "local-result")
        w.wait("completed goal ends supervision", lambda: decision_state(w, root).get("status") == "finished")
    else:
        w.wait("premature finish did not apply", lambda: runner_state(w, "invalid_output"))
        assert decision_state(w, root).get("status") != "finished"
        w.release(cp)
    assert not w.remote()["effects"]


def rate_limited(w):
    cp, _ = decision_setup(w, "rate_limit")
    before = w.wait("provider rate limit stored", lambda: runner_state(w, "provider_rate_limited"))
    w.program("restart", "observer")
    assert runner_state(w, "provider_rate_limited") == before
    assert len(w.sql("SELECT * FROM attempts")) == 1
    w.release(cp)


def remediation_decision(w, *, stale=False):
    from bootstrap import run, write_json
    from scenarios_remediation import _health, _initial, _repairs

    decision_config(w, "candidate" if stale else "wait")
    number, branch, head, base = _initial(w)
    action = "candidate" if stale else "wait"
    rule = w.fault("decision", {"action": action, "candidate_action": "repair_ci"}, "ready")
    _health(w, number, head, base)
    cp = w.checkpoint(rule, timeout=90)
    assert not _repairs(w)
    if stale:
        write_json(STATE / "decision-policy.json", {"action": "wait"})
        remote = f"--git-dir=/git/{REPO}.git"
        run("git", remote, "update-ref", f"refs/heads/{branch}", base)
        _health(w, number, base, base, result="success")
        w.wait(
            "old CI episode superseded by current head",
            lambda: w.sql(
                "SELECT * FROM pr_remediation_episodes WHERE observed_head_sha=? AND status='superseded'", head
            ),
        )
        w.release(cp)
        w.wait("old decision refused", lambda: runner_state(w, "stale") or runner_state(w, "decision_stale"))
        assert not _repairs(w)
    else:
        w.release(cp)
        w.wait(
            "wait decision is durable with no repair",
            lambda: w.sql(
                "SELECT * FROM supervision_checkpoints WHERE scope_type='decision_record' AND rule_id=?",
                cp["entity"]["bundle_id"],
            ),
        )
        assert not _repairs(w)
        write_json(STATE / "decision-policy.json", {"action": "candidate"})
        w.wait("selected offered candidate creates one repair", lambda: _repairs(w), timeout=90)
        assert len(_repairs(w)) == 1
        assert len(w.sql("SELECT * FROM pr_remediation_episodes WHERE episode_kind='ci'")) == 1

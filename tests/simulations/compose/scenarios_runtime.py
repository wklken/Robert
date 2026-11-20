"""Daemon ownership and process recovery on preserved storage."""

from datetime import datetime, timezone

from bootstrap import REPO
from observation import group, process
from scenarios_delivery import completed, effect_rows, setup
from seeds import issue, world


def daemon_lease(w):
    rows = w.sql("SELECT * FROM leases WHERE resource_type='daemon' AND status='active'")
    assert len(rows) == 1, rows
    return rows[0]


def restart_expired(w, lease):
    expiry = datetime.fromisoformat(lease["expires_at"])
    w.note("lease_wait", owner_id=lease["owner_id"], expires_at=lease["expires_at"])
    w.wait("old daemon lease expires", lambda: (datetime.now(timezone.utc) - expiry).total_seconds() >= 2, timeout=190)
    w.program("start", "daemon")
    return w.wait(
        "different daemon owner",
        lambda: next(
            (
                row
                for row in w.sql("SELECT * FROM leases WHERE resource_type='daemon' AND status='active'")
                if row["owner_id"] != lease["owner_id"]
            ),
            None,
        ),
    )


def daemon_restart(w, *, scanner=False):
    setup(w)
    worker = w.fault("worker", {"expected_output": "comment_analysis"}, "ready", seconds=300)
    w.start()
    cp = w.checkpoint(worker)
    identity = cp["entity"]
    old_worker = process(identity["pid"])
    observer = process(w.pid("observer"))
    old_daemon = process(w.pid("daemon"))
    if scanner:
        rule = w.fault("notifications", {}, "before_effect", seconds=300)
        scan = w.checkpoint(rule)
        pgid = scan["actor"]["pgid"]
        leader = process(pgid)
        assert leader and "--child" in leader["argv"]
        # Prevent the old scanner from returning on its own deadline before
        # the new daemon can exercise registered-child recovery.
        import os
        import signal

        os.killpg(pgid, signal.SIGSTOP)
        w.note("signal", target=leader, signal="SIGSTOP", whole_group=True)
    else:
        w.wait(
            "no active controller child",
            lambda: not w.sql("SELECT * FROM github_control_children WHERE status IN ('running','starting')"),
        )
    lease = daemon_lease(w)
    w.kill(old_daemon, whole_group=False)
    assert process(identity["pid"])["start_token"] == old_worker["start_token"]
    restart_expired(w, lease)
    if scanner:
        w.wait("old scanner process group reaped by the new daemon", lambda: not group(pgid))
        w.release(scan, "abort")
    assert process(observer["pid"])["start_token"] == observer["start_token"]
    assert process(identity["pid"])["start_token"] == old_worker["start_token"]
    assert len(w.sql("SELECT * FROM attempts WHERE task_id=?", identity["task_id"])) == 1
    w.release(cp)
    completed(w, "comment-analysis")
    assert len(effect_rows(w, "comment")) == 1


def daemon_competition(w):
    from bootstrap import CONFIG
    import json

    w.seed(world(issue_data=issue(actor="outsider")))
    w.policy()
    w.start()
    old = daemon_lease(w)
    command = ["robert", "daemon", "run", "--config", str(CONFIG), "--once"]
    import subprocess

    result = subprocess.run(command, text=True, capture_output=True, timeout=15)
    assert result.returncode != 0, result.stdout
    details = json.loads(result.stdout)
    assert "lease" in json.dumps(details).lower(), details
    assert daemon_lease(w)["owner_id"] == old["owner_id"]
    assert not effect_rows(w, "comment")


def dual_lane(w, *, publisher=False):
    setup(w)
    if publisher:
        hold = w.fault("comment", {"repo": REPO, "number": 11}, "before_effect")
        w.start()
        cp = w.checkpoint(hold)
        # A Web event does not depend on the blocked publishing lane.
        status, created = w.post(
            "/api/work-items",
            {
                "repo_id": "repo:example/other",
                "title": "Independent local task",
                "description": "Analyze the fixture arithmetic.",
                "mode": "start",
            },
        )
        assert status == 201, created
        w.wait(
            "local result during held publication",
            lambda: w.sql("SELECT * FROM worker_results WHERE output_type='local_result'"),
            timeout=17,
        )
        assert not effect_rows(w, "comment")
        w.release(cp)
        completed(w, "comment-analysis")
    else:
        worker = w.fault("worker", {"expected_output": "comment_analysis"}, "ready")
        w.start()
        ready = w.checkpoint(worker)
        hold = w.fault("notifications", {}, "before_effect")
        cp = w.checkpoint(hold)
        w.release(ready)
        w.wait("publish while scan is blocked", lambda: effect_rows(w, "comment"), timeout=20)
        assert group(cp["actor"]["pgid"])
        w.release(cp)


def recreate(w, *, crash=False):
    from bootstrap import STATE, write_json

    setup(w)
    if crash:
        rule = w.fault("worker", {"expected_output": "comment_analysis"}, "ready", seconds=300)
    w.start()
    if crash:
        cp = w.checkpoint(rule)
        actor = cp["entity"]
        w.wait(
            "no controller subprocess active",
            lambda: not w.sql("SELECT * FROM github_control_children WHERE status IN ('running','starting')"),
        )
        write_json(STATE / "recreate.json", {"lease": daemon_lease(w), "worker": actor})
    else:
        completed(w, "comment-analysis")
        write_json(STATE / "recreate.json", {"lease": daemon_lease(w), "worker": None})
    return "kill-recreate" if crash else "stop-recreate"


def resume_recreated(w):
    from bootstrap import STATE
    from seeds import thread
    import json

    state = json.loads((STATE / "recreate.json").read_text())
    worker = state["worker"]
    if worker:
        if group(worker["pgid"]):
            from driver import HarnessError

            raise HarnessError("old Worker group identifier is occupied in the new PID namespace")
        restart_expired(w, state["lease"])
        w.policy()
        w.patch({"repositories": {REPO: {"issues": {"12": issue(12)}}}, "notifications": [thread(REPO, 12)]})
        w.wait(
            "new request after abrupt container recreation",
            lambda: any(effect["entity"]["number"] == 12 for effect in effect_rows(w, "comment")),
        )
    else:
        assert w.sql("SELECT * FROM leases WHERE lease_id=? AND status='released'", state["lease"]["lease_id"])
        w.start()
        w.wait(
            "new scanner observes preserved state",
            lambda: w.sql(
                "SELECT * FROM daemon_events WHERE event_type='scan_completed' AND daemon_run_id!=?",
                state["lease"]["owner_id"],
            ),
        )
        assert len(effect_rows(w, "comment")) == 1
        assert len(w.sql("SELECT * FROM worker_results WHERE output_type='comment_analysis'")) == 1


def terminate_stack(w):
    setup(w)
    rule = w.fault("worker", {"expected_output": "comment_analysis"}, "ready")
    w.start()
    cp = w.checkpoint(rule)
    assert group(cp["entity"]["pgid"])
    return "stop-stack"


def capacity(w):
    from bootstrap import OTHER
    from client import fixture
    from seeds import thread

    seed = world()
    for name, numbers in ((REPO, (11, 12)), (OTHER, (21, 22))):
        for number in numbers:
            seed["repositories"][name]["issues"][str(number)] = issue(number, repo=name)
            seed["notifications"].append(thread(name, number))
    w.seed(seed)
    w.policy()

    def configure(cfg):
        cfg["scheduler"]["max_concurrency"] = 2
        for repo in cfg["repos"]:
            repo["max_concurrency"] = 1

    w.config(configure)
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "ready", hits=4)
    w.start()

    def hits():
        return [row for row in fixture("/checkpoints?after_seq=0")["checkpoints"] if row["rule_id"] == hold]

    first = w.wait("two independently occupied repo slots", lambda: hits() if len(hits()) >= 2 else None)
    assert len(first) == 2
    assert {row["entity"]["workstream_id"].split("#", 1)[0] for row in first} == {f"github:{REPO}", f"github:{OTHER}"}
    assert all(group(row["entity"]["pgid"]) for row in first)
    for row in first:
        w.release(row)
    final = w.wait("all four workers reached ready", lambda: hits() if len(hits()) == 4 else None)
    for row in final[2:]:
        w.release(row)
    w.wait("one remote result per source", lambda: len(effect_rows(w, "comment")) == 4)
    assert len({(row["entity"]["repo"], row["entity"]["number"]) for row in effect_rows(w, "comment")}) == 4


def pause_local(w, *, timeout=False):
    from bootstrap import OTHER
    from seeds import thread

    setup(w)

    def configure(cfg):
        cfg["scheduler"]["max_concurrency"] = 2
        if timeout:
            cfg["supervision"]["hard_timeout_seconds"] = 120

    w.config(configure)
    before = w.fault("worker", {"expected_output": "comment_analysis"}, "before_submit")
    w.start()
    worker = w.checkpoint(before)
    rules = [w.fault("git-upload-pack", {"repo": OTHER}, "before_effect") for _ in range(3)]
    w.patch(
        {
            "repositories": {OTHER: {"issues": {"12": issue(12, repo=OTHER, coding=True)}}},
            "notifications": [thread(REPO, 11), thread(OTHER, 12)],
        }
    )
    for rule in rules:
        cp = w.checkpoint(rule)
        actor = process(cp["actor"]["pgid"])
        assert actor and any(arg.endswith("/runtime/loop.py") for arg in actor["argv"]), actor
        w.kill(actor, whole_group=True)
        w.release(cp, "abort")
    attention = w.wait(
        "local control budget latches",
        lambda: w.sql(
            "SELECT * FROM notifications WHERE metadata_json LIKE '%control_plane_failed%' "
            'AND metadata_json LIKE \'%"control_lane": "local"%\''
        ),
    )
    assert attention
    attempt = worker["entity"]["attempt_id"]
    if not timeout:
        w.release(worker)
        w.wait(
            "paused monitor recorded worker exit",
            lambda: (
                w.sql("SELECT * FROM worker_results WHERE attempt_id=?", attempt)
                and not group(worker["entity"]["pgid"])
            ),
        )
    else:
        w.wait("paused monitor enforced hard timeout", lambda: not group(worker["entity"]["pgid"]), timeout=130)
        assert w.sql("SELECT * FROM attempts WHERE attempt_id=? AND status='failed'", attempt)
    assert not w.remote()["effects"]

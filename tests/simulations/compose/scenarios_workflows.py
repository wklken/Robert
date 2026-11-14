"""Issue waiting and reply lifecycle through the real scanner."""

from bootstrap import REPO
from scenarios_delivery import effect_rows, published
from seeds import comment, issue, now, thread, world


def waiting(w, *, outsider=False, replay=False):
    data = issue()
    w.seed(world(issue_data=data))
    w.policy(classify="waiting-for-user", wait=True)
    hold = w.fault("comment", {"repo": REPO, "number": 11}, "before_effect")
    w.start()
    cp = w.checkpoint(hold)
    assert not effect_rows(w, "comment")
    assert not w.sql("SELECT * FROM tasks WHERE lifecycle='waiting_for_user'")
    w.release(cp)
    published(w)
    waiting_tasks = w.wait(
        "published operator question", lambda: w.sql("SELECT * FROM tasks WHERE lifecycle='waiting_for_user'")
    )
    parent = waiting_tasks[0]["task_id"]
    w.policy(classify="waiting-for-user" if replay else "comment-analysis", wait=replay)
    sent = w.remote()["world"]["repositories"][REPO]["issues"]["11"]["comments"]
    reply = comment(1103, actor="outsider" if outsider else "maintainer", body="@robot preserve the API.")
    sent.append(reply)
    w.patch({"repositories": {REPO: {"issues": {"11": {"comments": sent}}}}, "notifications": [thread(REPO, 11)]})
    if outsider:
        count = len(w.sql("SELECT 1 FROM daemon_events WHERE event_type='scan_completed'"))
        w.wait(
            "two post-reply scans",
            lambda: len(w.sql("SELECT 1 FROM daemon_events WHERE event_type='scan_completed'")) >= count + 2,
        )
        assert w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='waiting_for_user'", parent)
        assert not w.sql("SELECT * FROM tasks WHERE parent_task_id=?", parent)
        assert len(effect_rows(w, "comment")) == 1
        return
    next_tasks = w.wait(
        "a child consumed the trusted reply",
        lambda: w.sql(
            "SELECT t.* FROM tasks t LEFT JOIN tasks route ON t.parent_task_id=route.task_id "
            "WHERE (t.parent_task_id=? OR (route.parent_task_id=? AND route.route_id='classification-result')) "
            "AND t.lifecycle=? AND t.route_id!='classification-result'",
            parent,
            parent,
            "waiting_for_user" if replay else "completed",
        ),
    )
    assert len(next_tasks) == 1
    if replay:
        second = next_tasks[0]["task_id"]
        assert second != parent
        w.patch({"notifications": [thread(REPO, 11)]})
        count = len(w.sql("SELECT 1 FROM daemon_events WHERE event_type='scan_completed'"))
        w.wait(
            "two post-replay scans",
            lambda: len(w.sql("SELECT 1 FROM daemon_events WHERE event_type='scan_completed'")) >= count + 2,
        )
        assert w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='waiting_for_user'", second)
        assert not w.sql("SELECT * FROM tasks WHERE parent_task_id=?", second)


def classify(w):
    data = issue()
    data["comments"][0]["body"] = "@robot take a look and decide whether this needs changes."
    w.seed(world(issue_data=data))
    w.policy(classify="comment-analysis")
    w.start()
    published(w)
    classification = w.sql("SELECT * FROM tasks WHERE route_id='classification-result' AND lifecycle='completed'")
    assert len(classification) == 1
    assert not w.sql("SELECT * FROM github_actions WHERE task_id=?", classification[0]["task_id"])
    assert w.sql(
        "SELECT * FROM tasks WHERE parent_task_id=? AND route_id='comment-analysis'", classification[0]["task_id"]
    )


def open_followup(w):
    from bootstrap import run
    from scenarios_delivery import new_pr

    new_pr(w)
    created = effect_rows(w, "pr_create")[0]["effect"]
    number = created["number"]
    head = created["head"]["sha"]
    run("git", f"--git-dir=/git/{REPO}.git", "update-ref", f"refs/pull/{number}/head", head)
    w.policy(followup=True)
    hold = w.fault("worker", {"expected_output": "update_existing_pr"}, "after_submit")
    request = comment(7001, body="@robot update this PR to document the addition API.")
    w.patch(
        {
            "repositories": {REPO: {"pulls": {str(number): {"comments": [request]}}}},
            "notifications": [thread(REPO, number, kind="PullRequest")],
        }
    )
    return created, w.checkpoint(hold)


def update_pr(w, *, conflict=None):
    from bootstrap import STATE, run

    created, checkpoint = open_followup(w)
    number = created["number"]
    ref = created["head"]["ref"]
    old = created["head"]["sha"]
    actor = checkpoint["entity"]
    assert actor["status"] == "recorded", checkpoint
    remote = f"--git-dir=/git/{REPO}.git"
    if conflict == "head":
        checkout = STATE / "checkouts" / REPO
        tree = run("git", "rev-parse", "main^{tree}", cwd=checkout)
        alternative = run("git", "commit-tree", tree, "-p", "main", "-m", "Competing remote revision", cwd=checkout)
        run("git", remote, "update-ref", f"refs/heads/{ref}", alternative)
        expected_head = alternative
    elif conflict == "author":
        w.patch({"repositories": {REPO: {"pulls": {str(number): {"user": {"login": "outsider"}}}}}})
        expected_head = old
    elif conflict == "head_repo":
        w.patch({"repositories": {REPO: {"pulls": {str(number): {"head": {"repo": {"full_name": "example/other"}}}}}}})
        expected_head = old
    elif conflict == "push_url":
        attempt = w.sql("SELECT worktree_path FROM attempts WHERE attempt_id=?", actor["attempt_id"])[0]
        run(
            "git",
            "remote",
            "set-url",
            "--push",
            "origin",
            "git@github.com:example/other.git",
            cwd=attempt["worktree_path"],
        )
        expected_head = old
    w.release(checkpoint)
    if conflict:
        w.wait(
            "push blocked before changing the target",
            lambda: w.sql(
                "SELECT * FROM github_actions WHERE task_id=? AND action_type='push_existing_pr' "
                "AND (audit_status IN ('policy_violation','failed') OR json_extract(metadata_json,'$.publish.failed_at') IS NOT NULL)",
                actor["task_id"],
            ),
        )
        assert run("git", remote, "rev-parse", ref) == expected_head
        assert not w.sql(
            "SELECT * FROM github_actions WHERE task_id=? AND publish_status='published'", actor["task_id"]
        )
    else:
        w.wait(
            "the current PR was updated",
            lambda: w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='completed'", actor["task_id"]),
        )
        assert run("git", remote, "rev-parse", ref) != old
        assert len(effect_rows(w, "pr_create")) == 1 and len(effect_rows(w, "comment")) == 1
        w.patch(
            {
                "repositories": {
                    REPO: {"pulls": {str(number): {"state": "closed", "merged": True, "merged_at": now()}}}
                },
                "notifications": [thread(REPO, number, kind="PullRequest")],
            }
        )
        from client import WEB_URL, request

        item = w.item(actor["workstream_id"])
        from urllib.parse import quote

        path = "/api/work-items/" + quote(item["work_item_id"], safe="")
        w.wait("merged PR appears in done", lambda: request(WEB_URL + path)[1].get("column") == "done")


def close_source(w, *, pull=False, merged=False):
    from scenarios_delivery import setup

    if pull:
        created, checkpoint = open_followup(w)
        number = created["number"]
        source = {"state": "closed", "merged": merged, "merged_at": now() if merged else None}
        category = "pulls"
        kind = "PullRequest"
    else:
        setup(w, coding=True)
        rule = w.fault("worker", {"expected_output": "new_pr"}, "ready")
        w.start()
        checkpoint = w.checkpoint(rule)
        number, category, kind = 11, "issues", "Issue"
        source = {"state": "closed", "state_reason": "not_planned"}
    w.patch(
        {"repositories": {REPO: {category: {str(number): source}}}, "notifications": [thread(REPO, number, kind=kind)]}
    )
    actor = checkpoint["entity"]
    from observation import group

    w.wait("closed source stopped the current Worker", lambda: not group(actor["pgid"]))
    assert not w.sql("SELECT * FROM github_actions WHERE task_id=? AND publish_status='published'", actor["task_id"])
    assert len(effect_rows(w, "pr_create")) == int(pull)


def failed_push_retry(w):
    from bootstrap import run
    from pathlib import Path

    created, checkpoint = open_followup(w)
    actor = checkpoint["entity"]
    hook = Path(f"/git/{REPO}.git/hooks/pre-receive")
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    w.release(checkpoint)
    w.wait(
        "real rejected push leaves later action pending",
        lambda: w.sql(
            "SELECT * FROM github_actions WHERE task_id=? AND action_type='push_existing_pr' "
            "AND json_extract(metadata_json,'$.publish.failed_at') IS NOT NULL",
            actor["task_id"],
        ),
    )
    assert not effect_rows(w, "comment")
    ref = created["head"]["ref"]
    assert run("git", f"--git-dir=/git/{REPO}.git", "rev-parse", ref) == created["head"]["sha"]
    hook.unlink()
    item = w.item(actor["workstream_id"])
    status, response = w.item_command(item, "retry_publish")
    assert status == 200, response
    w.wait(
        "retry completed without new Worker attempt",
        lambda: w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='completed'", actor["task_id"]),
    )
    assert len(w.sql("SELECT * FROM attempts WHERE task_id=?", actor["task_id"])) == 1
    assert len(effect_rows(w, "comment")) == 1


def third_party(w):
    from scenarios_native import _seed, _PULL

    seed = _seed(w, review=True, eligible=False)
    pull = seed["repositories"][REPO]["pulls"][str(_PULL)]
    pull["comments"][0]["body"] = "@robot fix the arithmetic in this PR."
    w.patch({"repositories": {REPO: {"pulls": {str(_PULL): pull}}}})
    w.policy()
    w.start()
    published(w)
    assert not w.actions("push_existing_pr") and not effect_rows(w, "pr_create")
    assert len(effect_rows(w, "comment")) == 1


def named_worker(w):
    from bootstrap import HARNESS, STATE
    import sys

    w.seed(world(issue_data=issue()))
    w.policy()

    def configure(cfg):
        cfg["workers"]["alternate"] = {
            **cfg["workers"]["fixture"],
            "command": [sys.executable, str(HARNESS / "fake_worker.py"), str(STATE), "--name", "alternate"],
        }
        cfg["routes"] = {"comment-analysis": {"worker": "alternate"}}

    w.config(configure)
    rule = w.fault("worker", {"worker": "alternate", "expected_output": "comment_analysis"}, "ready")
    w.start()
    cp = w.checkpoint(rule)
    assert cp["entity"]["worker"] == "alternate"
    assert not [
        call
        for call in w.remote()["calls"]
        if call["operation"] == "worker" and call["entity"].get("worker") == "fixture"
    ]
    w.release(cp)
    published(w)


def handoff(w):
    import json

    data = issue()
    w.seed(world(issue_data=data))
    w.policy()
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "after_submit")
    w.start()
    first = w.checkpoint(hold)
    actor = first["entity"]
    followup = comment(1105, body="@robot also report the result for negative inputs.")
    w.patch(
        {
            "repositories": {REPO: {"issues": {"11": {"comments": [*data["comments"], followup]}}}},
            "notifications": [thread(REPO, 11)],
        }
    )
    w.wait(
        "new source event arrives before first Worker exits",
        lambda: w.sql("SELECT * FROM github_events WHERE event_fingerprint='comment:1105'"),
    )
    old = w.sql("SELECT consumed_event_fingerprints_json FROM worker_results WHERE attempt_id=?", actor["attempt_id"])
    assert "comment:1105" not in json.loads(old[0]["consumed_event_fingerprints_json"])
    w.release(first)
    w.program("restart", "daemon")
    child = w.wait(
        "the pending event gets one completed successor",
        lambda: w.sql("SELECT * FROM tasks WHERE parent_task_id=? AND lifecycle='completed'", actor["task_id"]),
    )
    assert len(child) == 1
    assert len(w.sql("SELECT * FROM attempts WHERE task_id=?", actor["task_id"])) == 1
    assert len(effect_rows(w, "comment")) == 2

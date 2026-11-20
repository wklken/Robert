"""Externally visible publication and its ambiguous failure boundaries."""

from bootstrap import REPO, run
from client import fixture
from seeds import issue, world


def setup(w, *, coding=False, **policy):
    w.seed(world(issue_data=issue(coding=coding)))
    w.policy(classify="new-pr" if coding else "comment-analysis", **policy)


def effect_rows(w, operation):
    return [row for row in w.remote()["effects"] if row["operation"] == operation]


def published(w, action="comment"):
    return w.wait(f"published {action}", lambda: w.actions(action, "published"))


def completed(w, route):
    return w.wait(
        f"{route} task completed",
        lambda: w.sql("SELECT * FROM tasks WHERE route_id=? AND lifecycle='completed'", route),
    )


def new_pr(w):
    setup(w, coding=True)
    w.start()
    rows = published(w, "open_pr")
    task = completed(w, "new-pr")
    remote = w.remote()["world"]["repositories"][REPO]["pulls"]
    assert len(remote) == 1, remote
    pull = next(iter(remote.values()))
    head = pull["head"]["ref"]
    expected_sha = run("git", f"--git-dir=/git/{REPO}.git", "rev-parse", head)
    assert pull["base"]["ref"] == "main"
    assert effect_rows(w, "pr_create")[0]["effect"]["head"]["sha"] == expected_sha
    run("git", f"--git-dir=/git/{REPO}.git", "merge-base", "--is-ancestor", "main", head)
    assert "return x + y" in run("git", f"--git-dir=/git/{REPO}.git", "show", f"{head}:calc.py")
    assert len(rows) == len(task) == 1, (rows, task)
    assert len(published(w, "push_existing_pr")) == 1
    assert len(effect_rows(w, "pr_create")) == 1
    assert w.sql("SELECT origin_workstream_id FROM workstreams WHERE origin_workstream_id IS NOT NULL")
    assert rows[0]["external_id"]
    from client import WEB_URL, request
    from urllib.parse import quote

    work_item = w.wait("issue board item with derived PR", lambda: w.item(f"github:{REPO}#11"))
    path = "/api/work-items/" + quote(work_item["work_item_id"], safe="")
    assert request(WEB_URL + path)[1]["column"] == "review"


def _crash(w, operation, phase):
    rule = w.fault(operation, {"repo": REPO}, phase)
    w.start()
    hit = w.checkpoint(rule)
    executing = w.sql("SELECT * FROM publication_intents WHERE status='executing'")
    assert len(executing) == 1, executing
    if phase == "after_effect_commit":
        assert hit["effect_id"] and len(effect_rows(w, operation)) == 1
    elif phase == "before_effect":
        assert not effect_rows(w, operation)
    else:
        assert hit["entity"]["new"]
    w.kill_publisher(hit)
    w.release(hit, "abort")
    return hit


def lost_comment(w):
    setup(w)
    _crash(w, "comment", "after_effect_commit")
    assert len(published(w)) == len(effect_rows(w, "comment")) == 1
    completed(w, "comment-analysis")
    assert len([row for row in w.remote()["calls"] if row["operation"] == "comment"]) == 1


def lost_pr(w):
    setup(w, coding=True)
    _crash(w, "pr_create", "after_effect_commit")
    assert len(published(w, "open_pr")) == len(effect_rows(w, "pr_create")) == 1
    completed(w, "new-pr")
    assert len([row for row in w.remote()["calls"] if row["operation"] == "pr_create"]) == 1


def lost_push(w):
    setup(w, coding=True)
    hit = _crash(w, "git-receive-pack", "post_receive")
    completed(w, "new-pr")
    assert len(published(w, "push_existing_pr")) == len(effect_rows(w, "pr_create")) == 1
    matching = [
        row
        for row in fixture("/checkpoints?after_seq=0")["checkpoints"]
        if row["operation"] == "git-receive-pack"
        and row["phase"] == "post_receive"
        and row["entity"]["ref"] == hit["entity"]["ref"]
    ]
    assert len(matching) == 1, matching


def confirmed_absent(w):
    setup(w)
    _crash(w, "comment", "before_effect")
    assert len(published(w)) == len(effect_rows(w, "comment")) == 1
    completed(w, "comment-analysis")


def rejected(w, mutation, *, coding=False):
    setup(w, coding=coding, invalid=mutation)
    hold = w.fault("worker", {"expected_output": "new_pr" if coding else "comment_analysis"}, "after_submit")
    w.start()
    hit = w.checkpoint(hold)
    attempt = hit["entity"]["attempt_id"]
    assert hit["entity"]["status"] == "failed_validation", hit
    assert not w.sql("SELECT * FROM worker_results WHERE attempt_id=?", attempt)
    assert not w.remote()["effects"]


def withdraw(w, *, effect=None):
    setup(w)
    if effect:
        phase = "after_effect_commit" if effect == "present" else "before_effect"
        rule = w.fault("comment", {"repo": REPO}, phase)
    else:
        rule = w.fault("worker", {"expected_output": "comment_analysis"}, "after_submit")
    w.start()
    hit = w.checkpoint(rule)
    w.config(lambda cfg: cfg["repos"][0].update(trusted_actors=["different-maintainer"]))
    if effect:
        w.kill_publisher(hit)
        w.release(hit, "abort")
    else:
        w.release(hit)
    if effect == "present":
        assert len(published(w)) == len(effect_rows(w, "comment")) == 1
    else:
        w.wait(
            "authorization failure before write",
            lambda: w.sql(
                "SELECT * FROM github_actions WHERE publish_status='not_published' "
                "AND json_extract(metadata_json,'$.publish.failed_at') IS NOT NULL "
                "AND json_extract(metadata_json,'$.publish.next_retry_at') IS NULL"
            ),
        )
        assert not effect_rows(w, "comment") and not w.actions("comment", "published")


def unknown_effect(w, variant):
    setup(w)
    phase = "after_effect_commit" if variant == "unreadable" else "before_effect"
    rule = w.fault("comment", {"repo": REPO, "number": 11}, phase)
    w.start()
    hit = w.checkpoint(rule)
    assert w.sql("SELECT * FROM publication_intents WHERE status='executing'")
    if variant == "unreadable":
        w.fault(
            "issue_comments",
            {"repo": REPO, "number": 11},
            "before_effect",
            {"kind": "http", "status": 503, "body": {"message": "Read unavailable"}},
            hits=100,
        )
    else:
        # Use the actual attempted body, including Robert's publication marker.
        call = next(row for row in w.remote()["calls"] if row["request_id"] == hit["request_id"])
        from seeds import comment

        body = next(arg.removeprefix("body=") for arg in call["argv"] if arg.startswith("body="))
        actor = "outsider" if variant == "author" else "robot"
        injected = comment(1199, actor=actor, body=body if variant == "author" else body + "\nChanged.")
        prior = w.remote()["world"]["repositories"][REPO]["issues"]["11"]["comments"]
        w.patch({"repositories": {REPO: {"issues": {"11": {"comments": [*prior, injected]}}}}})
    w.kill_publisher(hit)
    w.release(hit, "abort")
    w.wait(
        "ambiguous publication reaches attention",
        lambda: w.sql("SELECT * FROM publication_intents WHERE status='attention'"),
    )
    assert not w.actions("comment", "published")
    assert len(effect_rows(w, "comment")) == (1 if variant == "unreadable" else 0)


def transient_write(w, http_status):
    setup(w)
    effect = {"kind": "http", "status": http_status, "body": {"message": "Fixture write unavailable"}}
    if http_status == 429:
        effect["headers"] = {"Retry-After": "10"}
    rule = w.fault("comment", {"repo": REPO}, "before_effect", effect)
    w.start()
    cp = w.checkpoint(rule)
    assert cp["effect_id"] is None
    if http_status in {401, 403}:
        w.wait(
            "permanent publication failure",
            lambda: w.sql("SELECT * FROM publication_intents WHERE status IN ('failed','attention')"),
        )
        assert not effect_rows(w, "comment")
    else:
        assert len(published(w)) == len(effect_rows(w, "comment")) == 1
        assert len([row for row in w.remote()["calls"] if row["operation"] == "comment"]) == 2


def budget(w):
    setup(w)
    rule = w.fault(
        "comment",
        {"repo": REPO},
        "before_effect",
        {"kind": "http", "status": 503, "body": {"message": "Unreachable write"}},
        hits=3,
        seconds=200,
    )
    w.start()
    w.checkpoint(rule)
    w.wait(
        "publication budget exhausted",
        lambda: w.sql(
            "SELECT * FROM github_actions WHERE json_extract(metadata_json,'$.publish.reason_code')='retry_budget_exhausted'"
        ),
        timeout=195,
    )
    assert len([row for row in w.remote()["calls"] if row["operation"] == "comment"]) == 3
    assert not w.remote()["effects"]


def dry_run(w):
    setup(w, coding=True)
    w.program("start", "daemon-dry")
    w.wait("dry daemon ran a cycle", lambda: w.sql("SELECT * FROM daemon_events WHERE event_type='scan_completed'"))
    assert not w.remote()["effects"]
    from client import fixture

    assert not [cp for cp in fixture("/checkpoints?after_seq=0")["checkpoints"] if cp["phase"] == "post_receive"]


def redaction(w):
    value = "ghp_" + ("a2b3c4d5" * 5)
    setup(w, comment=f"Fixture output included {value}.")
    w.start()
    published(w)
    effects = effect_rows(w, "comment")
    assert len(effects) == 1
    assert value not in effects[0]["effect"]["body"]

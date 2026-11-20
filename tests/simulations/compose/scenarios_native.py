"""Native rounds exercised via actual GitHub ingestion, model processes and publication."""

from collections import Counter
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import quote

from bootstrap import DB, GIT, HARNESS, OTHER, REPO, STATE, run
from client import WEB_URL, fixture, request
from driver import HarnessError
from observation import group
from seeds import comment, issue, now, thread, world


_PULL = 42
_BRANCH = "fixture-review"
_WORKERS = ("model-a", "model-b", "model-c")


def _change_pull():
    checkout = STATE / "checkouts" / REPO
    if run("git", "branch", "--show-current", cwd=checkout) != _BRANCH:
        run("git", "switch", "-c", _BRANCH, cwd=checkout)
        if "upstream" not in run("git", "remote", cwd=checkout).splitlines():
            run("git", "remote", "add", "upstream", f"git@github.com:{REPO}.git", cwd=checkout)
    with (checkout / "calc.py").open("a") as stream:
        stream.write("\n# Synthetic review revision\n")
    run("git", "add", "calc.py", cwd=checkout)
    run("git", "commit", "-m", "Update fixture arithmetic context", cwd=checkout)
    run(
        "git",
        "push",
        str(GIT / f"{REPO}.git"),
        f"HEAD:refs/heads/{_BRANCH}",
        f"HEAD:refs/pull/{_PULL}/head",
        cwd=checkout,
    )
    return run("git", "rev-parse", "HEAD", cwd=checkout)


def _seed(w, *, review=False, vote=False, eligible=True, evidence=False):
    data = world()
    if review:
        stamp = now()
        request = comment(4201, body="@robot review this PR.")
        request["html_url"] = f"https://github.com/{REPO}/pull/{_PULL}#issuecomment-4201"
        data["repositories"][REPO]["pulls"][str(_PULL)] = {
            "user": {"login": "author" if eligible else "outside-author"},
            "title": "Review synthetic arithmetic change",
            "head": {"ref": _BRANCH, "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "created_at": stamp,
            "updated_at": stamp,
            "comments": [request],
        }
        data["notifications"].append(thread(REPO, _PULL, kind="PullRequest"))
    vote_repo = OTHER if review else REPO
    if vote:
        source = issue(repo=vote_repo)
        source["title"] = "Choose an event delivery mechanism"
        source["body"] = "We can choose polling or webhook delivery."
        source["comments"][0]["body"] = "@robot 投票：A 轮询，B webhook，按可靠性比较。"
        if vote_repo != REPO:
            source["comments"][0]["html_url"] = source["comments"][0]["html_url"].replace(REPO, vote_repo)
        if evidence:
            adopted = comment(1100, body="A polling interval can be tuned.")
            stamp = source["comments"][0]["created_at"]
            source["comments"].append({**adopted, "created_at": stamp, "updated_at": stamp})
        data["repositories"][vote_repo]["issues"]["11"] = source
        data["notifications"].append(thread(vote_repo, 11))
    w.seed(data)
    if review:
        _change_pull()
    return data


def _config(w, *, review=False, vote=False, voters=2, task_slots=1):
    def edit(config):
        config["scheduler"]["max_concurrency"] = task_slots
        config["scheduler"]["model_max_concurrency"] = 2
        for name in _WORKERS:
            config["workers"][name] = {
                "adapter": "command",
                "command": [sys.executable, str(HARNESS / "fake_models.py"), str(STATE), name],
                "model": name,
                "effort": "default",
                "timeout_seconds": 65,
            }
        routes = config.setdefault("routes", {})
        if review:
            routes["review-pr"] = {
                "worker": _WORKERS[0],
                "multi_agent_review": {"enabled": True, "workers": list(_WORKERS[:2]), "author_allowlist": ["author"]},
            }
        if vote:
            routes["vote"] = {
                "worker": _WORKERS[0],
                "multi_agent_vote": {"enabled": True, "workers": list(_WORKERS[:voters])},
            }

    w.config(edit)


def _sessions(w, workflow, status=None):
    rows = w.sql(f"SELECT * FROM {workflow}_sessions ORDER BY created_at")
    return [row for row in rows if status is None or row["status"] == status]


def _published(w, workflow):
    round_ = w.wait(f"{workflow} publication", lambda: _sessions(w, workflow, "published"), timeout=85)[0]
    assert len(_sessions(w, workflow)) == 1
    w.wait(
        "published native task finalized",
        lambda: w.sql("SELECT task_id FROM tasks WHERE task_id=? AND lifecycle='completed'", round_["task_id"]),
    )
    return round_


def _models():
    return [
        point
        for point in fixture("/checkpoints?after_seq=0")["checkpoints"]
        if point.get("operation") == "model" and point["phase"] == "ready"
    ]


def _model_counts():
    return Counter((point["entity"]["stage"], point["entity"]["worker"]) for point in _models())


def _verify_effect(w, operation):
    matches = [effect for effect in w.remote()["effects"] if effect["operation"] == operation]
    assert len(matches) == 1, matches
    return matches[0]["effect"]


def _assert_slot_released(w):
    assert not w.sql(
        "SELECT run_id FROM review_runs WHERE slot_reserved=1 UNION ALL SELECT run_id FROM vote_runs WHERE slot_reserved=1"
    )


def review(w, *, eligible=True):
    _seed(w, review=True, eligible=eligible)
    _config(w, review=True)
    w.policy()
    w.start()
    if not eligible:
        w.wait("single-worker review", lambda: w.actions("comment", "published"))
        _verify_effect(w, "comment")
        assert not _sessions(w, "review")
        assert not _models()
        return
    round_ = _published(w, "review")
    expected = json.loads(round_["manifest_json"])["head_sha"]
    posted = _verify_effect(w, "pr_review")
    assert posted["commit_id"] == expected
    assert posted["state"] == "COMMENTED"
    assert f"robert-review round={round_['round_id']}" in posted["body"]
    assert _model_counts() == {
        ("review.prepare", "model-a"): 1,
        ("review.evaluate", "model-a"): 1,
        ("review.evaluate", "model-b"): 1,
        ("review.synthesize", "model-a"): 1,
    }
    _assert_slot_released(w)


def _review_barriers(w):
    return [
        w.fault("model", {"stage": "review.evaluate", "worker": worker}, "ready", seconds=80) for worker in _WORKERS[:2]
    ]


def shared_slots(w):
    _seed(w, review=True)
    _config(w, review=True, vote=True, task_slots=2)
    w.policy()
    rules = _review_barriers(w)
    w.start()
    checkpoints = [w.checkpoint(rule) for rule in rules]
    # The vote arrives while both review processes already own model slots.
    data = issue(repo=OTHER)
    data["comments"][0]["body"] = "@robot 投票：A 轮询，B webhook，按可靠性比较。"
    data["comments"][0]["html_url"] = data["comments"][0]["html_url"].replace(REPO, OTHER)
    w.patch(
        {
            "repositories": {OTHER: {"issues": {"11": data}}},
            "notifications": [thread(REPO, _PULL, kind="PullRequest"), thread(OTHER, 11)],
        }
    )
    waiting = w.wait(
        "vote coordinator waits for a shared model slot",
        lambda: w.sql("SELECT * FROM vote_runs WHERE status='waiting' AND slot_reserved=0"),
    )
    assert len(waiting) == 1
    running = w.sql("SELECT * FROM review_runs WHERE status='running' AND slot_reserved=1")
    assert {row["run_id"] for row in running} == {point["entity"]["run_id"] for point in checkpoints}
    assert {json.loads(row["dispatch_json"])["pid"] for row in running} == {
        point["actor"]["pid"] for point in checkpoints
    }
    for point in checkpoints:
        assert group(point["actor"]["pgid"]), point
    assert not [point for point in _models() if point["entity"]["stage"].startswith("vote.")]
    assert len(_sessions(w, "vote")) == len(_sessions(w, "review")) == 1
    for point in checkpoints:
        w.release(point)
    _published(w, "review")
    _published(w, "vote")
    _assert_slot_released(w)


def _sealed(w, workflow, stage, worker):
    return w.sql(
        f"SELECT artifact_json FROM {workflow}_runs WHERE stage=? AND worker_id=? AND status='succeeded'", stage, worker
    )


def _verify_hashes(rows):
    assert rows
    for row in rows:
        entry = json.loads(row["artifact_json"])
        digests = {
            path: entry[key.removesuffix("_path") + "_sha256"]
            for key, path in entry.items()
            if key.endswith("_path") and key.removesuffix("_path") + "_sha256" in entry
        }
        assert digests, entry
        for path, digest in digests.items():
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest


def review_retry(w):
    _seed(w, review=True)
    _config(w, review=True)
    w.policy(bad_model=["review.evaluate", "model-b"])
    w.start()
    prior = w.wait("sealed successful sibling", lambda: _sealed(w, "review", "review", "model-a"))
    _published(w, "review")
    assert _model_counts() == {
        ("review.prepare", "model-a"): 1,
        ("review.evaluate", "model-a"): 1,
        ("review.evaluate", "model-b"): 2,
        ("review.synthesize", "model-a"): 1,
    }
    assert len(w.sql("SELECT run_id FROM review_runs WHERE worker_id='model-b' AND status='failed'")) == 1
    assert _sealed(w, "review", "review", "model-a") == prior
    _verify_hashes(prior)
    _verify_effect(w, "pr_review")


def vote_retry(w):
    _seed(w, vote=True)
    _config(w, vote=True)
    w.policy(bad_model=["vote.synthesize", "model-a"])
    w.start()
    prior = {worker: w.wait("sealed ballot", lambda: _sealed(w, "vote", "vote", worker)) for worker in _WORKERS[:2]}
    _published(w, "vote")
    assert _model_counts() == {
        ("vote.prepare", "model-a"): 1,
        ("vote.cast", "model-a"): 1,
        ("vote.cast", "model-b"): 1,
        ("vote.synthesize", "model-a"): 2,
    }
    assert len(w.sql("SELECT run_id FROM vote_runs WHERE stage='synthesis' AND status='failed'")) == 1
    for worker, rows in prior.items():
        assert _sealed(w, "vote", "vote", worker) == rows
        _verify_hashes(rows)
    _verify_effect(w, "comment")


def clarified_vote(w):
    data = _seed(w, vote=True)
    _config(w, vote=True, voters=3)
    w.policy(clarify_vote=True, ballots={"model-a": "A", "model-b": "A", "model-c": "B"})
    w.start()
    waiting = w.wait("vote is waiting for clarification", lambda: _sessions(w, "vote", "waiting_for_user"))[0]
    w.wait("clarification delivered", lambda: w.actions("comment", "published"))
    question = _verify_effect(w, "comment")
    assert "What is the latency target?" in question["body"]
    source = data["repositories"][REPO]["issues"]["11"]
    reply = comment(1102, body="@robot latency target is one second.")
    w.patch(
        {
            "repositories": {
                REPO: {
                    "issues": {
                        "11": {
                            "updated_at": now(),
                            "comments": [*source["comments"], question, reply],
                        }
                    }
                }
            },
            "notifications": [thread(REPO, 11, identifier="vote-clarification")],
        }
    )
    complete = _published(w, "vote")
    assert complete["round_id"] == waiting["round_id"]
    reports = [effect["effect"]["body"] for effect in w.remote()["effects"] if effect["operation"] == "comment"]
    assert len(reports) == 2
    assert "达到全体参与者的严格多数" in reports[-1]
    assert '{"A": 2, "B": 1}' in reports[-1]
    assert all(f"Reason from {worker}" in reports[-1] for worker in _WORKERS)


def tied_vote(w):
    _seed(w, vote=True)
    _config(w, vote=True, voters=3)
    w.policy(claim_consensus=True, ballots={"model-a": "A", "model-b": "B", "model-c": None})
    w.start()
    _published(w, "vote")
    body = _verify_effect(w, "comment")["body"]
    assert "平票，无胜出项" in body and '{"A": 1, "B": 1}' in body and "弃权 1" in body
    assert "> All models agree on option B." in body


def failed_vote(w):
    _seed(w, vote=True)
    _config(w, vote=True)
    w.policy(fail_model=["vote.cast", "model-b"])
    w.start()
    w.wait(
        "vote failure after recovery",
        lambda: w.sql("""
        SELECT s.round_id FROM vote_sessions s JOIN tasks t ON s.task_id=t.task_id
        WHERE t.lifecycle='failed' AND s.status='failed'
    """),
        timeout=100,
    )
    assert not w.remote()["effects"]
    assert not w.sql("SELECT * FROM vote_runs WHERE stage='synthesis'")
    _verify_hashes(_sealed(w, "vote", "vote", "model-a"))
    assert w.sql("SELECT * FROM vote_runs WHERE worker_id='model-b' AND status='failed'")
    _assert_slot_released(w)
    round_ = _sessions(w, "vote")[0]
    item = w.sql("SELECT * FROM work_items WHERE workstream_id=?", round_["workstream_id"])[0]
    status, detail = request(WEB_URL + "/api/work-items/" + quote(item["work_item_id"], safe=""))
    assert status == 200 and detail["task_lifecycle"] == "failed", detail
    assert "model-b" in detail["vote"]["error"]
    task = quote(round_["task_id"], safe="")
    status, result = w.post(f"/api/tasks/{task}/retrigger", {})
    assert status == 202 and result["status"] == "queued", result
    assert result["task_id"] == round_["task_id"] and result["wakeup_id"]
    assert not w.remote()["effects"]


def cancel_review(w):
    _seed(w, review=True)
    _config(w, review=True)
    w.policy()
    rules = _review_barriers(w)
    w.start()
    points = [w.checkpoint(rule) for rule in rules]
    round_ = _sessions(w, "review")[0]
    w.wait("native identities registered", lambda: len(w.sql("SELECT * FROM review_runs WHERE status='running'")) == 2)
    item = w.sql("SELECT * FROM work_items WHERE workstream_id=?", round_["workstream_id"])[0]
    path = f"/api/work-items/{quote(item['work_item_id'], safe='')}/commands"
    status, result = w.post(path, {"command": "cancel", "expected_version": item["version"]})
    assert status == 200, result
    w.wait("round canceled", lambda: _sessions(w, "review", "canceled"))
    for point in points:
        assert not group(point["actor"]["pgid"])
        w.release(point, "abort")
    assert not w.remote()["effects"]
    assert not w.sql("SELECT * FROM review_runs WHERE stage='synthesis'")
    _assert_slot_released(w)


@contextmanager
def _publication_lock(w, task_id):
    directory = Path(str(DB.resolve()) + ".publication-locks")
    directory.mkdir(exist_ok=True, mode=0o700)
    name = hashlib.sha256(task_id.encode()).hexdigest()
    with (directory / f"{name}.lock").open("a") as handle:

        def acquire():
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return True

        w.wait("publication fault lock acquired", acquire, timeout=10, error=HarnessError)
        yield


def _stale(w, workflow):
    data = _seed(w, review=workflow == "review", vote=workflow == "vote", evidence=True)
    _config(w, review=workflow == "review", vote=workflow == "vote")
    w.policy()
    rule = w.fault("model", {"stage": f"{workflow}.synthesize"}, "output_ready", seconds=80)
    w.start()
    point = w.checkpoint(rule)
    round_ = _sessions(w, workflow)[0]
    with _publication_lock(w, round_["task_id"]):
        w.release(point)
        ready = w.wait("native report ready under publication lock", lambda: _sessions(w, workflow, "ready"))[0]
        w.wait("audited publication action", lambda: w.actions(status="not_published"))
        assert not w.remote()["effects"]
        if workflow == "review":
            head = _change_pull()
            assert head != json.loads(ready["manifest_json"])["head_sha"]
        else:
            manifest = json.loads(ready["manifest_json"])
            assert "comment:1100" in manifest["sources"]
            original = data["repositories"][REPO]["issues"]["11"]["comments"][0]
            w.patch({"repositories": {REPO: {"issues": {"11": {"comments": [original]}}}}})
    w.wait("obsolete native snapshot superseded", lambda: _sessions(w, workflow, "superseded"), timeout=75)
    assert not w.remote()["effects"]


def stale_review(w):
    _stale(w, "review")


def stale_vote(w):
    _stale(w, "vote")

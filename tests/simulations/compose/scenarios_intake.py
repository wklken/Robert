"""Discovery, deduplication, and edited-comment authority."""

from scenarios_delivery import completed, effect_rows, published
from seeds import issue, world


def _scan_count(w):
    return len(w.sql("SELECT 1 FROM daemon_events WHERE event_type='scan_completed'"))


def discover_once(w, *, assigned=False):
    w.seed(world(issue_data=issue(assigned=assigned), assigned=assigned))
    w.policy()
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "ready")
    w.start()
    cp = w.checkpoint(hold)
    w.wait("three scanner passes with repeated notification", lambda: _scan_count(w) >= 3)
    w.release(cp)
    assert len(published(w)) == len(effect_rows(w, "comment")) == 1
    assert len(completed(w, "comment-analysis")) == 1
    assert len(w.sql("SELECT * FROM workstreams")) == 1


def unauthorized(w, mode):
    data = issue(actor="outsider" if mode == "outsider" else "maintainer", edited=mode != "outsider")
    seed = world(issue_data=data)
    if mode == "editor":
        seed["nodes"] = {
            data["comments"][0]["node_id"]: {
                "id": data["comments"][0]["node_id"],
                "body": data["comments"][0]["body"],
                "author": {"login": "maintainer"},
                "editor": {"login": "outsider"},
                "lastEditedAt": data["comments"][0]["updated_at"],
                "updatedAt": data["comments"][0]["updated_at"],
            }
        }
    w.seed(seed)
    w.policy()
    if mode == "missing_editor":
        rule = w.fault(
            "graphql", {}, "before_effect", {"kind": "http", "status": 503, "body": {"message": "Unavailable"}}
        )
    w.start()
    if mode == "missing_editor":
        w.checkpoint(rule)
    w.wait("two complete discovery cycles", lambda: _scan_count(w) >= 2)
    assert not w.sql("SELECT * FROM attempts") and not w.remote()["effects"]


def notification_pages(w, *, failure=False):
    import json
    from client import fixture
    from bootstrap import OTHER, REPO
    from seeds import thread

    seed = world(issue_data=issue(actor="outsider"))
    seed["repositories"][OTHER]["issues"]["12"] = issue(12, repo=OTHER, actor="outsider")
    seed["notifications"].append(thread(OTHER, 12))
    seed["notification_page_size"] = 1
    w.seed(seed)
    w.policy()
    w.start()

    def snapshot():
        rows = w.sql("SELECT payload_json FROM github_read_cache WHERE cache_key='notifications'")
        return json.loads(rows[0]["payload_json"]) if rows else None

    first = w.wait("both notification pages committed", snapshot)
    assert len(first["hints"]) == 2, first
    if failure:
        fault = w.fault(
            "notifications",
            {"page": 2},
            "before_effect",
            {"kind": "http", "status": 503, "body": {"message": "Fixture page failure"}},
            hits=100,
        )
        w.patch({"notifications": [thread(REPO, 11, identifier="changed"), thread(OTHER, 12)]})
        cp = w.checkpoint(fault)
        w.wait(
            "the scanner that read the failed page exits",
            lambda: w.sql(
                "SELECT * FROM github_control_children WHERE child_id=? AND status='stopped'",
                cp["actor"]["executor_id"],
            ),
        )
        assert snapshot() == first
        fixture(f"/faults/{fault}/disarm", {"scenario_id": w.scenario_id})
        next_snapshot = w.wait(
            "complete replacement snapshot",
            lambda: value if (value := snapshot()) and value["last_modified"] != first["last_modified"] else None,
            timeout=90,
        )
        assert len(next_snapshot["hints"]) == 2
    else:
        w.wait(
            "conditional poll receives 304",
            lambda: any(
                row["operation"] == "notifications" and "304" in str(row.get("outcome")) for row in w.remote()["calls"]
            ),
        )
        assert snapshot() == first
    assert not w.remote()["effects"]


def source_isolation(w):
    from bootstrap import OTHER, REPO
    from seeds import thread

    seed = world(issue_data=issue())
    seed["repositories"][OTHER]["issues"]["12"] = issue(12, repo=OTHER)
    seed["notifications"].append(thread(OTHER, 12))
    w.seed(seed)
    w.policy()
    rule = w.fault(
        "issue",
        {"repo": REPO, "number": 11},
        "before_effect",
        {"kind": "http", "status": 503, "body": {"message": "Fixture source failure"}},
        hits=100,
    )
    w.start()
    w.checkpoint(rule)
    published(w)
    rows = effect_rows(w, "comment")
    assert len(rows) == 1 and rows[0]["entity"]["repo"] == OTHER, rows
    assert not w.sql("SELECT * FROM tasks t JOIN workstreams s USING(workstream_id) WHERE s.repo_id=?", f"repo:{REPO}")


def low_search(w):

    data = issue()
    seed = world(issue_data=data)
    seed["rate_limit"] = {"search": {"remaining": 0}}
    w.seed(seed)
    w.policy()
    w.start()
    published(w)
    assert not [row for row in w.remote()["calls"] if row["operation"] == "search"]
    assert len(effect_rows(w, "comment")) == 1


def pending_reauthorization(w, *, exhaust=False):
    from bootstrap import CONFIG, REPO, run
    from seeds import comment, thread
    import sys

    w.seed(world(issue_data=issue(coding=True)))
    w.policy(classify="new-pr")
    failure = w.fault(
        "git-upload-pack",
        {"repo": REPO},
        "before_effect",
        {"kind": "exit", "exit_code": 1},
        hits=5 if exhaust else 1,
        seconds=800,
    )
    w.start()
    w.checkpoint(failure)

    def batch():
        rows = w.sql(
            "SELECT * FROM github_scan_batches WHERE repo_id=? AND retry_count>=? ORDER BY retry_count DESC",
            f"repo:{REPO}",
            5 if exhaust else 1,
        )
        return rows[0] if rows else None

    prior = w.wait("preparation failure retained the original batch", batch, timeout=720 if exhaust else 45)
    if exhaust:
        assert prior["attention_reason"] and prior["retry_count"] == 5
        result = run(
            sys.executable,
            "-B",
            "-m",
            "robert_agent.runtime.scan",
            "--config",
            str(CONFIG),
            "--retry-batch",
            prior["batch_id"],
        )
        assert '"ok": true' in result
        published(w, "open_pr")
        assert len(effect_rows(w, "pr_create")) == 1
    else:
        w.config(lambda cfg: cfg["repos"][0].update(trusted_actors=["new-maintainer"]))
        w.policy()
        fresh = comment(1109, actor="new-maintainer")
        fresh["author_association"] = "COLLABORATOR"
        w.patch(
            {
                "repositories": {
                    REPO: {
                        "permissions": {"new-maintainer": "write"},
                        "issues": {
                            "11": {
                                "comments": [
                                    issue(coding=True)["comments"][0],
                                    fresh,
                                ]
                            }
                        },
                    }
                },
                "notifications": [thread(REPO, 11)],
            }
        )
        w.program("restart", "daemon")
        published(w)
        assert len(effect_rows(w, "comment")) == 1 and not effect_rows(w, "pr_create")
        assert w.sql("SELECT batch_id FROM github_scan_batches WHERE batch_id=?", prior["batch_id"])

"""End-to-end PR remediation against real refs and synthetic check producers."""

import json

from bootstrap import GIT, REPO, STATE, run
from scenarios_delivery import effect_rows, new_pr
from seeds import now


_CHECK = "unit-tests"
_REMEDIATION = ("merge_conflict_remediation", "ci_remediation")


def _enable(w, *, conflict=False, max_attempts=2):
    skills = STATE / "skills"
    for name, behavior in (
        ("fast-preflight", "Run the fixture arithmetic check before delivery."),
        ("fast-conflict-fix", "Merge the pinned base and resolve the fixture conflict."),
        ("fast-test-fix", "Add and run the fixture arithmetic regression test."),
    ):
        path = skills / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\n---\n{behavior}\n")

    def edit(config):
        config["skills"] = {"search_paths": [str(skills)]}
        repo = next(repo for repo in config["repos"] if repo["full_name"] == REPO)
        repo["pr_automation"] = {
            "conflict": {"enabled": conflict},
            "ci": {"enabled": True, "check_allowlist": [_CHECK], "max_attempts": max_attempts},
        }

    w.config(edit)


def _git(*args):
    return run("git", "--git-dir", str(GIT / f"{REPO}.git"), *args)


def _head(branch):
    return _git("rev-parse", "refs/heads/" + branch)


def _initial(w, *, conflict=False, max_attempts=2):
    _enable(w, conflict=conflict, max_attempts=max_attempts)
    new_pr(w)
    effect = effect_rows(w, "pr_create")[0]["effect"]
    number, branch = effect["number"], effect["head"]["ref"]
    head = _head(branch)
    assert head == effect["head"]["sha"]
    _git("update-ref", f"refs/pull/{number}/head", head)
    return number, branch, head, _head("main")


def _base_conflict():
    checkout = STATE / "checkouts" / REPO
    assert run("git", "branch", "--show-current", cwd=checkout) == "main"
    (checkout / "calc.py").write_text("def add(x, y):\n    return x * y\n")
    run("git", "add", "calc.py", cwd=checkout)
    run("git", "commit", "-m", "Diverge the main fixture", cwd=checkout)
    run("git", "push", "origin", "main", cwd=checkout)
    return _head("main")


def _health(w, number, head, base, *, name=_CHECK, result="failure", mergeable=True, foreign=False):
    run_id = 901 if result == "failure" else 902
    fields = {
        "head": {"sha": head},
        "base": {"sha": base},
        "mergeable": mergeable,
        "updated_at": now(),
    }
    if foreign:
        # Keep the local origin binding and exercise the fresh remote author check.
        fields["user"] = {"login": "outside-author"}
    w.patch(
        {
            "repositories": {
                REPO: {
                    "pulls": {str(number): fields},
                    "check_runs": {
                        head: [
                            {
                                "id": run_id,
                                "name": name,
                                "status": "completed",
                                "conclusion": result,
                                "app": {"id": 31, "slug": "fixture-ci"},
                                "head_sha": head,
                                "details_url": f"https://github.com/{REPO}/runs/{run_id}",
                                "completed_at": now(),
                                "output": {"summary": "Missing regression coverage for addition."}
                                if result == "failure"
                                else {},
                            }
                        ],
                    },
                    "annotations": {"901": [{"path": "calc.py", "start_line": 1, "message": "Verify addition."}]},
                }
            }
        }
    )


def _repairs(w):
    return w.sql("SELECT * FROM tasks WHERE task_kind IN (?,?) ORDER BY created_at, task_id", *_REMEDIATION)


def _episodes(w, kind, status=None):
    rows = w.sql("SELECT * FROM pr_remediation_episodes WHERE episode_kind=? ORDER BY first_seen_at", kind)
    return [row for row in rows if status is None or row["status"] == status]


def _repair_pushed(w, kind, head, base, branch):
    episode = w.wait(
        f"{kind} waits for new remote signal",
        lambda: _episodes(w, kind, "waiting_for_signal"),
        timeout=110,
    )[0]
    assert episode["observed_head_sha"] == head and episode["observed_base_sha"] == base
    assert episode["attempt_count"] == 1 and episode["result_head_sha"] != head
    result = _head(branch)
    assert result == episode["result_head_sha"]
    _git("merge-base", "--is-ancestor", head, result)
    action = w.actions("push_existing_pr", "published")
    assert len(action) == 2 and any(row["task_id"] == episode["last_task_id"] for row in action), action
    audit = json.loads(
        w.sql("SELECT metadata_json FROM worker_results WHERE task_id=?", episode["last_task_id"])[0]["metadata_json"]
    )["audit"]
    attestation = audit["remediation_attestation"]
    assert attestation["status"] == "accepted"
    assert (attestation["head_sha"], attestation["base_sha"], attestation["result_head_sha"]) == (head, base, result)
    w.wait(
        "repair task finalized",
        lambda: w.sql("SELECT task_id FROM tasks WHERE task_id=? AND lifecycle='completed'", episode["last_task_id"]),
    )
    assert len(effect_rows(w, "pr_create")) == 1
    return episode, result


def _resolved(w, episode):
    rows = w.wait(
        "same remediation episode resolved",
        lambda: w.sql(
            "SELECT * FROM pr_remediation_episodes WHERE episode_id=? AND status='resolved'", episode["episode_id"]
        ),
        timeout=75,
    )
    assert len(rows) == 1 and rows[0]["attempt_count"] == 1


def conflict_first(w):
    number, branch, head, old_base = _initial(w, conflict=True)
    base = _base_conflict()
    assert base != old_base
    hold = w.fault("worker", {"expected_output": "update_existing_pr"}, "before_submit")
    _health(w, number, head, base, mergeable=False)
    point = w.checkpoint(hold, timeout=90)
    tasks = _repairs(w)
    assert len(tasks) == 1 and tasks[0]["task_kind"] == "merge_conflict_remediation", tasks
    assert tasks[0]["task_id"] == point["entity"]["task_id"]
    assert len(_episodes(w, "merge_conflict")) == len(_episodes(w, "ci")) == 1
    w.release(point)
    episode, result = _repair_pushed(w, "merge_conflict", head, base, branch)
    parents = _git("rev-list", "--parents", "-n", "1", result).split()
    assert parents == [result, head, base], parents
    _git("update-ref", f"refs/pull/{number}/head", result)
    _health(w, number, result, base, result="success")
    _resolved(w, episode)
    assert len(_repairs(w)) == len(_episodes(w, "merge_conflict")) == 1


def ci_fix(w):
    number, branch, head, base = _initial(w)
    _health(w, number, head, base)
    episode, result = _repair_pushed(w, "ci", head, base, branch)
    assert len(_repairs(w)) == 1 and _repairs(w)[0]["task_kind"] == "ci_remediation"
    assert w.sql("SELECT * FROM pr_ci_observations WHERE episode_id=? AND conclusion='failure'", episode["episode_id"])
    assert not _episodes(w, "ci", "resolved")
    _git("update-ref", f"refs/pull/{number}/head", result)
    _health(w, number, result, base, result="success")
    _resolved(w, episode)
    assert len(_episodes(w, "ci")) == len(_repairs(w)) == 1


def denied(w, *, foreign=False):
    number, branch, head, base = _initial(w)
    before = len(w.remote()["calls"])
    _health(w, number, head, base, name=_CHECK if foreign else "unchecked", foreign=foreign)
    operation = "pull" if foreign else "check_runs"

    def matching():
        return [
            call
            for call in w.remote()["calls"][before:]
            if call["operation"] == operation
            and call["entity"].get("repo") == REPO
            and (not foreign or call["entity"].get("number") == number)
            and call["outcome"] is not None
        ]

    w.wait("two fresh scans observed the disallowed PR health", lambda: len(matching()) >= 2, timeout=75)
    assert all(call["outcome"]["exit_code"] == 0 for call in matching())
    assert not _repairs(w)
    assert not _episodes(w, "ci") and not _episodes(w, "merge_conflict")
    assert _head(branch) == head and len(w.actions("push_existing_pr", "published")) == 1


def exhausted(w):
    number, branch, head, base = _initial(w, max_attempts=1)
    # An observed repeated signature after push has a separate terminal reason.
    # Exit before push to leave the same episode eligible for the attempt guard.
    w.note("exhaustion_trigger", operation="registered worker exits before publication", head_sha=head)
    w.policy(exit_without_result=True)
    rule = w.fault("worker", {"expected_output": "update_existing_pr"}, "ready")
    _health(w, number, head, base)
    point = w.checkpoint(rule)
    registered = w.wait(
        "remediation Worker registered before exit",
        lambda: w.sql(
            """SELECT attempt_id FROM attempts
               WHERE attempt_id=? AND status='running'
                 AND json_extract(metadata_json, '$.dispatch.process_start_token') IS NOT NULL""",
            point["entity"]["attempt_id"],
        ),
    )
    assert len(registered) == 1
    w.release(point)
    episode = w.wait("same-head CI attempt budget exhausted", lambda: _episodes(w, "ci", "exhausted"), timeout=115)[0]
    assert episode["observed_head_sha"] == head and episode["observed_base_sha"] == base
    assert episode["attempt_count"] == 1 and not episode["result_head_sha"]
    assert json.loads(episode["metadata_json"])["last_transition_reason"] == "max_attempts"
    tasks = _repairs(w)
    assert len(tasks) == 1 and tasks[0]["lifecycle"] == "failed"
    assert _head(branch) == head and len(w.actions("push_existing_pr", "published")) == 1
    assert len(_episodes(w, "ci")) == 1

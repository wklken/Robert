"""TAPD mention admission, GitHub delivery and ticket receipt reconciliation."""

from datetime import datetime, timedelta, timezone
import json

from bootstrap import HARNESS, REPO, run
from scenarios_delivery import completed, effect_rows
from seeds import world
from tapd_protocol import timestamp


def lost_comment(w):
    workspace, ticket = "100", "42"
    stamp = timestamp()
    initial = world()
    initial["tapd"] = {
        workspace: {
            "writer": "maintainer",
            "tickets": {
                "story": {
                    ticket: {
                        "id": ticket,
                        "workspace_id": workspace,
                        "name": "Fix the fixture arithmetic",
                        "description": "Addition must preserve integer callers.",
                        "status": "open",
                        "label": "fixture-repo",
                        "workitem_type_id": "1",
                        "created": stamp,
                    },
                },
            },
            "comments": [
                {
                    "id": comment_id,
                    "workspace_id": workspace,
                    "entry_type": "stories",
                    "entry_id": ticket,
                    "author": author,
                    "description": "@fixture-robert fix addition in calc.py and open a PR.",
                    "created": stamp,
                }
                for comment_id, author in (("17", "maintainer"), ("18", "outsider"))
            ],
        }
    }
    w.seed(initial)
    w.config(
        lambda cfg: cfg.update(
            tapd={
                "enabled": True,
                "executable": str(HARNESS / "fake_tapd.py"),
                "mention_name": "fixture-robert",
                "projects": [
                    {
                        "host": "tapd.example",
                        "workspace_id": workspace,
                        "trusted_user": "maintainer",
                        "accept_after": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                        "label_rules": [{"label": "fixture-repo", "repository": REPO}],
                    }
                ],
            }
        )
    )
    w.policy(classify="new-pr", tapd_result=True)
    w.fault("tapd_comment", {"workspace": workspace, "ticket": ticket}, "after_effect_commit", {"kind": "disconnect"})
    w.start()
    (task,) = completed(w, "new-pr", timeout=135)
    assert task["workstream_id"] == f"tapd:tapd.example/{workspace}/story/{ticket}:{REPO}"
    (pr,) = effect_rows(w, "pr_create")
    (reply,) = effect_rows(w, "tapd_comment")
    assert pr["entity"]["repo"] == REPO
    assert pr["effect"]["html_url"] in reply["effect"]["description"]
    assert reply["effect"]["description"].startswith("fixture-robert：")
    head = pr["effect"]["head"]
    assert run("git", f"--git-dir=/git/{REPO}.git", "rev-parse", head["ref"]) == head["sha"]
    assert not effect_rows(w, "comment")
    failures = [row for row in w.remote()["calls"] if row["operation"] == "tapd_comment"]
    assert len(failures) == 1 and failures[0]["outcome"] == {"disconnected": True}
    (action,) = w.actions("comment", "published")
    receipt = json.loads(action["metadata_json"])["publish"]
    assert receipt["deduplicated"] and str(action["external_id"]) == reply["effect"]["id"]

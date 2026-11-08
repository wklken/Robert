"""Synthetic GitHub participants and canonical event inputs."""

from datetime import datetime, timezone

from bootstrap import ACCOUNT, OTHER, REPO


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def comment(number=1101, *, actor="maintainer", body="@robot analyze the fixture addition.", edited=False):
    stamp = now()
    return {
        "id": number,
        "node_id": f"fixture-comment-{number}",
        "body": body,
        "user": {"login": actor},
        "author_association": "COLLABORATOR" if actor == "maintainer" else "NONE",
        "created_at": "2026-01-01T00:00:00Z" if edited else stamp,
        "updated_at": stamp,
        "html_url": f"https://github.com/{REPO}/issues/11#issuecomment-{number}",
    }


def thread(repo, number, *, identifier=None, kind="Issue", reason="mention"):
    return {
        "id": identifier or f"{repo}-{number}",
        "reason": reason,
        "updated_at": now(),
        "unread": True,
        "repository": {"full_name": repo},
        "subject": {
            "type": kind,
            "url": f"https://api.github.com/repos/{repo}/{'issues' if kind == 'Issue' else 'pulls'}/{number}",
        },
    }


def issue(number=11, *, repo=REPO, actor="maintainer", coding=False, assigned=False, edited=False):
    stamp = now()
    body = "@robot fix the addition in calc.py and open a PR." if coding else "@robot analyze the fixture addition."
    return {
        "number": number,
        "title": "Fixture addition uses subtraction",
        "body": "Inspect the fixture arithmetic.",
        "user": {"login": "maintainer"},
        "state": "open",
        "created_at": stamp,
        "updated_at": stamp,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "assignees": [{"login": ACCOUNT}] if assigned else [],
        "comments": [] if assigned else [comment(100 * number + 1, actor=actor, body=body, edited=edited)],
        "timeline": [
            {
                "id": 100 * number + 2,
                "event": "assigned",
                "actor": {"login": actor},
                "assignee": {"login": ACCOUNT},
                "created_at": stamp,
            }
        ]
        if assigned
        else [],
    }


def world(*, issue_data=None, repo=REPO, assigned=False):
    repositories = {
        name: {"permissions": {"maintainer": "write", "outsider": "read", ACCOUNT: "admin"}, "issues": {}, "pulls": {}}
        for name in (REPO, OTHER)
    }
    notifications = []
    if issue_data:
        number = issue_data["number"]
        repositories[repo]["issues"][str(number)] = issue_data
        notifications.append(thread(repo, number, reason="assign" if assigned else "mention"))
    return {"account": ACCOUNT, "repositories": repositories, "notifications": notifications}

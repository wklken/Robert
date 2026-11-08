"""Operator message authentication and receipts through the installed CLI."""

import json
import shutil
import subprocess

from bootstrap import CONFIG, OTHER, REPO
from observation import group, process
from seeds import world


_IDENTITY = {
    "protocol": "robert.message.v1",
    "provider": "openclaw",
    "channel": "telegram",
    "account": "fixture",
    "conversation": "telegram:42",
    "thread": "",
    "sender_id": "42",
}


def _cli(command, message):
    result = subprocess.run(
        ["robert", "messages", command, "--output", "json", "--config", str(CONFIG)],
        input=json.dumps(message),
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    return result.returncode, json.loads(result.stdout)


def _inbox():
    code, result = _cli("inbox", _IDENTITY)
    assert code == 0 and result["ok"], result
    return result["messages"]


def _reply(notice, *, version=None, body="Preserve the existing API.", action="reply"):
    return {
        **_IDENTITY,
        "notification_id": notice["notification_id"],
        "version": notice["version"] if version is None else version,
        "action": action,
        "body": body,
    }


def _created(w, repo, title):
    repo_id = next(item for item in w.session()["allowed_repo_ids"] if item.endswith(repo))
    status, result = w.post(
        "/api/work-items",
        {
            "repo_id": repo_id,
            "title": title,
            "description": "Analyze the arithmetic behavior.",
            "mode": "start",
            "priority": "P2",
            "routing_mode": "auto",
        },
    )
    assert status == 201, result
    return result["work_item_id"]


def _setup(w, *, repo=REPO, observer=False):
    # Valid synthetic authenticated input can never invoke a real messaging binary.
    assert shutil.which("openclaw") is None
    w.seed(world())
    w.policy(wait=True)

    def edit(config):
        config["messaging"] = {
            "enabled": True,
            **{key: _IDENTITY[key] for key in ("provider", "channel", "account", "conversation", "thread")},
            "target": "42",
            "allowed_senders": ["42"],
            "repos": [REPO],
        }

    w.config(edit)
    w.start(observer=observer)
    item = _created(w, repo, "Ask about fixture arithmetic")
    return _question(w, item)


def _question(w, item, *, newer_than=None):
    def observed():
        return w.sql(
            """
            SELECT n.notification_id,w.version,t.task_id,e.event_id
              FROM work_items w
              JOIN work_item_events e ON e.work_item_id=w.work_item_id AND e.event_type='operator_question'
              JOIN notifications n ON n.notification_id=('attention-' || e.event_id)
              JOIN tasks t ON t.task_id=n.task_id AND t.lifecycle='waiting_for_user'
             WHERE w.work_item_id=? AND e.event_id<>?
             ORDER BY e.created_at DESC,e.event_id DESC
        """,
            item,
            newer_than or "",
        )

    return w.wait("real operator question and durable outbox notice", observed)[0]


def _responses(w):
    return w.sql("SELECT * FROM work_item_events WHERE event_type='user_response' ORDER BY event_id")


def _waiting(w, notice):
    assert w.sql("SELECT task_id FROM tasks WHERE task_id=? AND lifecycle='waiting_for_user'", notice["task_id"])
    assert not w.sql("SELECT task_id FROM tasks WHERE parent_task_id=?", notice["task_id"])
    assert not w.remote()["effects"]


def refused(w, mode):
    notice = _setup(w, repo=OTHER if mode == "repo" else REPO)
    command = _reply(notice)
    visible_ids = {item["notification_id"] for item in _inbox()}
    assert (notice["notification_id"] in visible_ids) == (mode != "repo")
    if mode == "provider":
        command["provider"] = "other-provider"
    elif mode == "session":
        command["conversation"] = "telegram:43"
    elif mode == "sender":
        command["sender_id"] = "43"
    code, result = _cli("respond", command)
    expected = "Message not found." if mode == "repo" else "Messaging identity is not authorized."
    assert code != 0 and result["status"] == "rejected" and result["safe_error"] == expected, result
    assert not _responses(w)
    _waiting(w, notice)


def replay(w):
    notice = _setup(w)
    w.policy()
    command = _reply(notice)
    code, result = _cli("respond", command)
    assert code == 0 and result["status"] == "accepted" and result["task_id"], result
    assert _cli("respond", command) == (0, result)
    w.wait(
        "the one admitted child completed",
        lambda: w.sql("SELECT task_id FROM tasks WHERE task_id=? AND lifecycle='completed'", result["task_id"]),
    )
    assert _cli("respond", command) == (0, result)
    assert len(w.sql("SELECT task_id FROM tasks WHERE parent_task_id=?", notice["task_id"])) == 1
    response = _responses(w)
    assert len(response) == 1 and response[0]["resolves_event_id"] == notice["event_id"]
    assert response[0]["actor_identity"] == "openclaw:telegram:fixture:42"
    assert notice["notification_id"] not in {item["notification_id"] for item in _inbox()}
    assert not w.remote()["effects"]


def old_question(w):
    first = _setup(w)
    code, result = _cli("respond", _reply(first))
    assert code == 0 and result["status"] == "accepted", result
    item_id = w.sql("SELECT work_item_id FROM work_items")[0]["work_item_id"]
    second = _question(w, item_id, newer_than=first["event_id"])
    assert second["task_id"] == result["task_id"] and second["version"] > first["version"]
    code, rejected = _cli(
        "respond",
        _reply(first, version=second["version"], body="A different reply to the first question."),
    )
    assert code != 0 and rejected["status"] == "rejected", rejected
    assert len(_responses(w)) == 1
    _waiting(w, second)
    assert second["notification_id"] in {item["notification_id"] for item in _inbox()}


def ack_only(w):
    question = _setup(w, observer=True)
    code, rejected = _cli("respond", _reply(question, action="ack", body="Investigating."))
    assert code != 0 and rejected["status"] == "rejected", rejected

    w.policy()
    hold = w.fault("worker", {"expected_output": "local_result"}, "ready")
    _created(w, REPO, "Observe a separately stopped worker")
    point = w.checkpoint(hold)
    attempt = point["entity"]["attempt_id"]
    task = point["entity"]["task_id"]
    w.wait(
        "second worker has recorded identity",
        lambda: w.sql(
            """
            SELECT attempt_id FROM attempts WHERE attempt_id=?
              AND json_extract(metadata_json,'$.dispatch.process_start_token') IS NOT NULL
        """,
            attempt,
        ),
    )
    w.program("stop", "daemon")
    worker = process(point["actor"]["pid"])
    assert worker and group(worker["pgid"])
    w.kill(worker, whole_group=True)
    w.release(point, "abort")

    def incident_notice():
        return next(
            (
                item
                for item in _inbox()
                if item["type"] == "supervision_incident"
                and item["task_id"] == task
                and item["reason_code"] == "worker_exited_without_result"
            ),
            None,
        )

    notice = w.wait("observer exposes acknowledgement-only stopped-worker incident", incident_notice, timeout=90)
    assert notice["actions"] == ["ack"]
    command = _reply(notice, action="ack", body="Investigating locally.")
    code, received = _cli("respond", command)
    assert code == 0 and received["status"] == "acknowledged", received
    assert _cli("respond", command) == (0, received)
    metadata = w.sql("SELECT metadata_json FROM notifications WHERE notification_id=?", notice["notification_id"])[0]
    receipt = json.loads(metadata["metadata_json"])["operator_response"]
    assert receipt["action"] == "ack" and receipt["body"] == command["body"]
    assert w.sql(
        "SELECT incident_id FROM supervision_incidents WHERE incident_id=? AND status='open'", notice["incident_id"]
    )
    assert not group(worker["pgid"])
    assert not w.sql("SELECT task_id FROM tasks WHERE parent_task_id=?", task)
    assert not w.sql("SELECT * FROM supervision_recovery_actions")
    assert not _responses(w)
    _waiting(w, question)
    assert question["notification_id"] in {item["notification_id"] for item in _inbox()}

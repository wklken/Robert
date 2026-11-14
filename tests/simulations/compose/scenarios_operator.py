"""Loopback Web API tests with real session and persistence."""

from urllib.parse import quote
from uuid import uuid4

from bootstrap import REPO
from seeds import world


def setup(w):
    w.seed(world())
    w.policy()
    w.start()
    session = w.session()
    repo = next(item for item in session["allowed_repo_ids"] if item.endswith(REPO))
    return {
        "repo_id": repo,
        "title": "Inspect fixture addition",
        "description": "Analyze arithmetic behavior.",
        "mode": "backlog",
        "priority": "P2",
        "routing_mode": "auto",
    }


def csrf(w):
    payload = setup(w)
    status, result = w.post("/api/work-items", payload, omit=("X-Robert-CSRF-Token",))
    assert status == 403 and result["safe_error"] == "invalid CSRF token", result
    assert not w.sql("SELECT * FROM work_items")


def idempotency(w):
    payload = setup(w)
    key = uuid4().hex
    status, result = w.post("/api/work-items", payload, key=key)
    assert status == 201, result
    item = w.sql("SELECT * FROM work_items")[0]
    path = "/api/work-items/" + quote(item["work_item_id"], safe="") + "/commands"
    command = {"command": "start", "expected_version": item["version"]}
    command_key = uuid4().hex
    status, result = w.post(path, command, key=command_key)
    assert status == 200, result
    w.program("restart", "web")
    w.wait("web after restart", w.session)
    assert w.post("/api/work-items", payload, key=key)[0] == 201
    assert w.post(path, command, key=command_key)[0] == 200
    assert len(w.sql("SELECT * FROM work_items")) == 1
    w.wait("one local result", lambda: w.sql("SELECT * FROM worker_results WHERE output_type='local_result'"))
    assert len(w.sql("SELECT * FROM worker_results WHERE output_type='local_result'")) == 1
    assert not w.remote()["effects"]


def refused(w, mode):
    payload = setup(w)
    if mode == "origin":
        status, result = w.post("/api/work-items", payload, Origin="http://127.0.0.1:9")
        expected = 403
    elif mode == "host":
        status, result = w.post("/api/work-items", payload, Host="example.invalid")
        expected = 403
    elif mode == "key":
        status, result = w.post("/api/work-items", payload, omit=("X-Idempotency-Key",))
        expected = 422
    elif mode == "json":
        from urllib.error import HTTPError
        from urllib.request import urlopen, Request
        from client import WEB_URL

        req = Request(
            WEB_URL + "/api/work-items",
            data=b"{",
            headers={
                "Origin": WEB_URL,
                "Content-Type": "application/json",
                "X-Idempotency-Key": "invalid-json",
                "X-Robert-CSRF-Token": w.session()["csrf_token"],
            },
        )
        try:
            with urlopen(req, timeout=5) as response:
                status, result = response.status, response.read()
        except HTTPError as exc:
            with exc:
                status, result = exc.code, exc.read()
        expected = 422
    else:
        raise ValueError(mode)
    assert status == expected, result
    assert not w.sql("SELECT * FROM work_items") and not w.sql("SELECT * FROM work_item_events")


def version_conflict(w):
    payload = setup(w)
    status, created = w.post("/api/work-items", payload)
    assert status == 201, created
    item = w.sql("SELECT * FROM work_items")[0]
    path = "/api/work-items/" + quote(item["work_item_id"], safe="") + "/commands"
    status, result = w.post(
        path,
        {
            "command": "edit",
            "expected_version": item["version"] - 1,
            "title": "Changed by stale editor",
            "description": "Stale input.",
        },
    )
    assert status == 409, result
    assert w.sql("SELECT * FROM work_items")[0] == item


def read_only(w):
    payload = setup(w)
    w.program("stop", "web")
    w.program("start", "readonly")
    session = w.wait("read-only session", w.session)
    assert not session["writes_enabled"]
    status, result = w.post("/api/work-items", payload)
    assert status == 503, result
    assert not w.sql("SELECT * FROM work_items")


def cancel_replay(w):
    payload = setup(w)
    rule = w.fault("worker", {"expected_output": "local_result"}, "before_submit")
    status, created = w.post("/api/work-items", {**payload, "mode": "start"})
    assert status == 201, created
    cp = w.checkpoint(rule)
    item = w.item(cp["entity"]["workstream_id"])
    key = uuid4().hex
    status, result = w.cancel_item(item, key=key)
    assert status == 200, result
    assert w.item_command(item, "cancel", key=key)[0] == 200
    assert w.item(cp["entity"]["workstream_id"])["canceled_at"]
    assert w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='canceled'", cp["entity"]["task_id"])
    assert not w.remote()["effects"]


def retry_replay(w):
    payload = setup(w)
    w.policy(exit_without_result=True)
    rule = w.fault("worker", {"expected_output": "local_result"}, "ready")
    status, created = w.post("/api/work-items", {**payload, "mode": "start"})
    assert status == 201, created
    cp = w.checkpoint(rule)
    w.wait(
        "worker identity durably registered",
        lambda: w.sql(
            "SELECT attempt_id FROM attempts WHERE attempt_id=? AND json_extract(metadata_json,'$.dispatch.process_start_token') IS NOT NULL",
            cp["entity"]["attempt_id"],
        ),
    )
    w.release(cp)
    prior = w.wait("failed initial execution", lambda: w.sql("SELECT * FROM tasks WHERE lifecycle='failed'"))[0]
    w.policy()
    item = w.item(prior["workstream_id"])
    key = uuid4().hex
    status, result = w.item_command(item, "retry", key=key)
    assert status == 200, result
    assert w.item_command(item, "retry", key=key)[0] == 200
    retry = w.wait(
        "one completed successor with a recorded local result",
        lambda: w.sql(
            "SELECT t.task_id FROM tasks t JOIN worker_results r ON r.task_id=t.task_id "
            "WHERE t.parent_task_id=? AND t.lifecycle='completed' AND r.output_type='local_result'",
            prior["task_id"],
        ),
    )
    assert len(retry) == 1
    assert len(w.sql("SELECT * FROM attempts WHERE task_id=?", prior["task_id"])) == 1
    assert len(w.sql("SELECT * FROM worker_results WHERE output_type='local_result'")) == 1


def reply_replay(w, *, stale=False):
    payload = setup(w)
    w.policy(wait=True)
    status, created = w.post("/api/work-items", {**payload, "mode": "start"})
    assert status == 201, created
    parent = w.wait(
        "local task waiting for user", lambda: w.sql("SELECT * FROM tasks WHERE lifecycle='waiting_for_user'")
    )[0]
    item = w.item(parent["workstream_id"])
    events = w.sql("SELECT * FROM work_item_events WHERE work_item_id=? ORDER BY created_at DESC", item["work_item_id"])
    question = next(event for event in events if event["event_type"] == "operator_question")
    w.policy()
    key = uuid4().hex
    arguments = {
        "key": key,
        "expected_event_id": "question-obsolete" if stale else question["event_id"],
        "body": "Preserve the API.",
    }
    status, result = w.item_command(item, "reply", **arguments)
    if stale:
        assert status == 409, result
        assert w.sql("SELECT * FROM tasks WHERE task_id=? AND lifecycle='waiting_for_user'", parent["task_id"])
        assert not w.sql("SELECT * FROM tasks WHERE parent_task_id=?", parent["task_id"])
        return
    assert status == 200, result
    assert w.item_command(item, "reply", **arguments)[0] == 200
    w.program("restart", "web")
    w.wait("web after reply replay restart", w.session)
    assert w.item_command(item, "reply", **arguments)[0] == 200
    child = w.wait(
        "one completed reply",
        lambda: w.sql("SELECT * FROM tasks WHERE parent_task_id=? AND lifecycle='completed'", parent["task_id"]),
    )
    assert len(child) == 1


def notification(w, *, transport=False):
    from bootstrap import STATE
    from observation import query
    from scenarios_delivery import effect_rows, published, setup

    setup(w, wait=True)
    w.start(observer=not transport)
    if transport:
        w.program("start", "observer-notify")
    published(w)
    assert len(effect_rows(w, "comment")) == 1
    if not transport:
        w.wait(
            "delivery is not configured",
            lambda: w.sql(
                "SELECT * FROM notifications WHERE json_extract(metadata_json,'$.delivery.status')='not_configured'"
            ),
        )
        assert not (STATE / "notification-sink.sqlite3").exists()
        assert w.sql("SELECT * FROM tasks WHERE lifecycle='waiting_for_user'")
        return
    sink = STATE / "notification-sink.sqlite3"

    def retry_observed():
        return sink.exists() and query(sink, "SELECT id FROM calls GROUP BY id HAVING count(*) >= 2")

    repeated = w.wait("notification retried with the original delivery ID", retry_observed, timeout=75)
    assert len(repeated) == 1
    assert len(query(sink, "SELECT * FROM effects WHERE id=?", (repeated[0]["id"],))) == 1
    w.wait(
        "notification marked sent",
        lambda: w.sql("SELECT * FROM notifications WHERE json_extract(metadata_json,'$.delivery.status')='sent'"),
    )


def board_progress(w):
    from client import WEB_URL, request
    from scenarios_delivery import setup, completed

    setup(w)
    hold = w.fault("worker", {"expected_output": "comment_analysis"}, "ready")
    publish = w.fault("comment", {}, "before_effect")
    w.start()
    worker = w.checkpoint(hold)
    item = w.wait("board item for running task", lambda: w.item(worker["entity"]["workstream_id"]))
    path = "/api/work-items/" + quote(item["work_item_id"], safe="")

    def detail():
        status, result = request(WEB_URL + path)
        assert status == 200, result
        return result

    w.wait("board shows running attempt", lambda: detail().get("execution", {}).get("status") == "running")
    w.release(worker)
    pending = w.checkpoint(publish)
    assert detail()["publication"]["pending"] == 1
    w.release(pending)
    completed(w, "comment-analysis")
    assert detail()["publication"]["pending"] == 0
    assert detail()["task_id"] == worker["entity"]["task_id"]

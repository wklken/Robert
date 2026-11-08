"""In-container driver for independent Compose lifecycle cases."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import signal
import sqlite3
import time
from urllib.error import URLError
from urllib.parse import quote
from uuid import uuid4

from bootstrap import CONFIG, DB, STATE, SUPERVISOR, run, write_json
from client import WEB_URL, fixture, request
from observation import group, process, query


class HarnessError(RuntimeError):
    """The requested setup or fault was not executed."""


class World:
    def __init__(self, case_id):
        self.case_id = case_id
        self.rule_ids = []
        self.timeline = STATE / "timeline.jsonl"

    def note(self, action, **details):
        with self.timeline.open("a") as stream:
            print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "action": action, **details}), file=stream)

    def wait(self, label, predicate, *, timeout=45, error=AssertionError):
        until = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < until:
            try:
                value = predicate()
            except (URLError, FileNotFoundError) as exc:
                last_error = str(exc)
            except sqlite3.OperationalError as exc:
                if not any(word in str(exc).lower() for word in ("locked", "busy", "unable to open")):
                    raise
                last_error = str(exc)
            else:
                if value:
                    self.note("observed", label=label)
                    return value
            time.sleep(0.2)
        raise error(f"timed out waiting for {label}; last read error: {last_error}")

    def seed(self, seed):
        self.scenario_id = fixture(
            "/scenario",
            {
                "case_id": self.case_id,
                "run_id": uuid4().hex,
                "seed": seed,
            },
        )["scenario_id"]

    def patch(self, patch):
        return fixture("/seed-events", {"scenario_id": self.scenario_id, "patch": patch})

    def fault(self, operation, entity, phase, effect=None, *, seconds=180, hits=1):
        rule = fixture(
            "/faults",
            {
                "scenario_id": self.scenario_id,
                "operation": operation,
                "entity": entity,
                "phase": phase,
                "effect": effect or {"kind": "hold"},
                "max_hits": hits,
                "expires_after_seconds": seconds,
            },
        )["rule_id"]
        self.rule_ids.append(rule)
        self.note("armed", rule_id=rule, operation=operation, entity=entity, phase=phase)
        return rule

    def checkpoint(self, rule, *, timeout=45):
        return self.wait(
            f"rule {rule}",
            lambda: next(
                (cp for cp in fixture("/checkpoints?after_seq=0")["checkpoints"] if cp["rule_id"] == rule), None
            ),
            timeout=timeout,
            error=HarnessError,
        )

    def release(self, cp, decision="continue"):
        fixture(f"/barriers/{cp['barrier_id']}/release", {"decision": decision})
        self.note("barrier_released", checkpoint=cp, decision=decision)

    def remote(self):
        return fixture("/state")

    def sql(self, statement, *params):
        return query(DB, statement, params)

    def program(self, command, role=None):
        args = (role,) if role else ()
        result = run("supervisorctl", "-c", str(SUPERVISOR), command, *args)
        self.note("supervisor", command=command, role=role, result=result)
        return result

    def pid(self, role):
        pid = int(self.program("pid", role))
        if pid < 2:
            raise HarnessError(f"{role} is not running")
        return pid

    def start(self, *, web=True, observer=True):
        self.program("start", "daemon")

        def active_lease():
            if not self.sql("SELECT name FROM sqlite_master WHERE type='table' AND name='leases'"):
                return None
            return self.sql("SELECT owner_id FROM leases WHERE resource_type='daemon' AND status='active'")

        self.wait("daemon lease", active_lease, error=HarnessError)
        if observer:
            self.program("start", "observer")
        if web:
            self.program("start", "web")
            self.wait("web session", self.session)

    @contextmanager
    def suspend_daemon(self):
        actor = process(self.pid("daemon"))
        if not actor or not any("robert" in arg for arg in actor["argv"]):
            raise HarnessError("cannot identify the supervised daemon")
        pid = actor["pid"]
        self.note("signal", target=actor, signal="SIGSTOP", whole_group=False)
        os.kill(pid, signal.SIGSTOP)
        try:
            self.wait(
                "daemon suspended", lambda: (process(pid) or {}).get("state") == "T", timeout=5, error=HarnessError
            )
            yield actor
        finally:
            current = process(pid)
            if current and current["start_token"] == actor["start_token"] and current["state"] != "Z":
                os.kill(pid, signal.SIGCONT)
                self.note("signal", target=actor, signal="SIGCONT", whole_group=False)

    def kill(self, actor, *, whole_group):
        pid = actor["pid"]
        current = process(pid)
        if not current or current["start_token"] != actor["start_token"] or current["pgid"] != actor["pgid"]:
            raise HarnessError(f"process identity changed before kill: {actor}")
        if whole_group and current["pgid"] != pid:
            raise HarnessError(f"expected a process group leader: {actor}")
        self.note("signal", target=current, signal="SIGKILL", whole_group=whole_group)
        if whole_group:
            os.killpg(pid, signal.SIGKILL)
            self.wait("old process group stopped", lambda: not group(pid), timeout=5, error=HarnessError)
        else:
            os.kill(pid, signal.SIGKILL)
            self.wait(
                "old process stopped",
                lambda: not process(pid) or process(pid)["state"] == "Z",
                timeout=5,
                error=HarnessError,
            )

    def kill_publisher(self, checkpoint):
        rows = self.sql("SELECT * FROM github_control_children WHERE lane='publish' AND status='running'")
        if len(rows) != 1:
            raise HarnessError(f"expected one live publisher: {rows}")
        identity = json.loads(rows[0]["identity_json"])
        pid = int(identity["pid"])
        actor = process(pid)
        if (
            actor is None
            or "--child" not in actor["argv"]
            or str(identity.get("process_start_token")) != actor["start_token"]
            or identity.get("process_group_id") != actor["pgid"]
        ):
            raise HarnessError(f"missing registered publication wrapper: {identity}")
        held = checkpoint["actor"]
        caller = process(held["pid"])
        if (
            held.get("executor_id") != rows[0]["child_id"]
            or held["pgid"] != actor["pgid"]
            or caller is None
            or caller["pgid"] != actor["pgid"]
            or caller["state"] == "Z"
        ):
            raise HarnessError(f"held request is no longer owned by the registered publisher: {checkpoint}")
        self.kill(actor, whole_group=True)

    def config(self, edit):
        config = json.loads(CONFIG.read_text())
        edit(config)
        write_json(CONFIG, config)

    def policy(self, **policy):
        write_json(STATE / "worker-policy.json", policy)

    def session(self):
        status, data = request(WEB_URL + "/api/session")
        return data if status == 200 else None

    def post(self, path, payload, *, key=None, omit=(), **extra):
        session = self.session()
        headers = {
            "Origin": WEB_URL,
            "X-Robert-CSRF-Token": session["csrf_token"],
            "X-Idempotency-Key": key or uuid4().hex,
        }
        headers.update(extra)
        for name in omit:
            headers.pop(name)
        return request(WEB_URL + path, payload, headers=headers)

    def actions(self, action=None, status=None):
        return [
            row
            for row in self.sql("SELECT * FROM github_actions ORDER BY created_at")
            if (action is None or row["action_type"] == action) and (status is None or row["publish_status"] == status)
        ]

    def item(self, workstream_id):
        rows = self.sql("SELECT * FROM work_items WHERE workstream_id=?", workstream_id)
        return rows[0] if rows else None

    def item_command(self, item, command, **kwargs):
        path = "/api/work-items/" + quote(item["work_item_id"], safe="") + "/commands"
        key = kwargs.pop("key", None)
        return self.post(path, {"command": command, "expected_version": item["version"], **kwargs}, key=key)

    def cancel_item(self, item, *, key=None):
        key = key or uuid4().hex

        def request_when_unlocked():
            status, result = self.item_command(item, "cancel", key=key)
            if (
                status == 409
                and result.get("safe_error")
                == "A publication is in progress for this task. Retry cancellation after it finishes."
            ):
                return None
            return status, result

        return self.wait("cancel acquires the task publication lock", request_when_unlocked, timeout=10)

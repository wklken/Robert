"""Transactional remote state and fault barriers for the Compose fixtures."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from email.utils import formatdate
import json
import sqlite3
import time
from uuid import uuid4


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _merge(target, patch):
    for key, value in patch.items():
        if key.startswith("_"):
            raise ValueError("reserved fixture field")
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _clock():
    return datetime.now(timezone.utc).isoformat()


class FixtureStore:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS scenario (
                    id TEXT PRIMARY KEY, initial_seed TEXT, world TEXT
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, data TEXT, outcome TEXT
                );
                CREATE TABLE IF NOT EXISTS effects (
                    seq INTEGER PRIMARY KEY, data TEXT
                );
                CREATE TABLE IF NOT EXISTS faults (
                    id TEXT PRIMARY KEY, data TEXT, hits INTEGER, expires REAL
                );
                CREATE TABLE IF NOT EXISTS barriers (
                    id TEXT PRIMARY KEY, rule_id TEXT, decision TEXT
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    seq INTEGER PRIMARY KEY, data TEXT
                );
                CREATE TABLE IF NOT EXISTS failures (
                    seq INTEGER PRIMARY KEY, data TEXT
                );
            """)

    @contextmanager
    def _transaction(self, *, write=True):
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn

    @staticmethod
    def _scenario(conn):
        row = conn.execute("SELECT id, world FROM scenario").fetchone()
        if row is None:
            raise ValueError("seed the fixture first")
        return row[0], json.loads(row[1])

    def seed(self, run_id, case_id, seed):
        if not all(isinstance(value, str) and value for value in (run_id, case_id)):
            raise ValueError("run_id and case_id must be nonempty strings")
        scenario_id = f"{run_id}:{case_id}"
        if not isinstance(seed, dict) or any(key.startswith("_") for key in seed):
            raise ValueError("invalid fixture seed")
        encoded_seed = _json(seed)
        with self._transaction() as conn:
            previous = conn.execute("SELECT id, initial_seed FROM scenario").fetchone()
            if previous:
                if previous != (scenario_id, encoded_seed):
                    raise ValueError("fixture database already owns a different scenario or seed")
                return scenario_id
            world = {
                "account": "robot",
                "repositories": {},
                "notifications": [],
                **seed,
                "_timestamp": _clock(),
                "_notification_version": int(time.time()),
                "_next_id": 1_000_000,
            }
            conn.execute("INSERT INTO scenario VALUES (?, ?, ?)", (scenario_id, encoded_seed, _json(world)))
        return scenario_id

    def patch(self, scenario_id, patch):
        if not isinstance(patch, dict):
            raise ValueError("seed event patch must be an object")
        with self._transaction() as conn:
            current_id, world = self._scenario(conn)
            if current_id != scenario_id:
                raise ValueError("wrong scenario")
            _merge(world, patch)
            if "notifications" in patch:
                world["_notification_version"] += 1
            conn.execute("UPDATE scenario SET world = ?", (_json(world),))
            self._checkpoint(conn, {"phase": "seed_event", "seed_actor": True, "patch": patch})

    def arm(self, rule):
        rule_id = uuid4().hex
        expires_after = rule.get("expires_after_seconds", 60)
        max_hits = rule.get("max_hits", 1)
        entity = rule.get("entity")
        effect = rule.get("effect")
        if isinstance(effect, str):
            effect = {"kind": effect.lower()}
        if (
            not isinstance(rule.get("operation"), str)
            or not isinstance(rule.get("phase"), str)
            or not isinstance(entity, dict)
            or not isinstance(effect, dict)
            or effect.get("kind") not in {"hold", "http", "disconnect", "exit"}
            or type(max_hits) is not int
            or not 1 <= max_hits <= 100
            or type(expires_after) not in {int, float}
            or not 0 < expires_after <= 900
        ):
            raise ValueError("invalid fault rule")
        if effect["kind"] == "http" and (
            type(effect.get("status")) is not int
            or not 100 <= effect["status"] <= 599
            or not isinstance(effect.get("headers", {}), dict)
        ):
            raise ValueError("invalid HTTP fault")
        if effect["kind"] == "exit" and (
            type(effect.get("exit_code")) is not int
            or not 0 <= effect["exit_code"] <= 255
            or any(not isinstance(effect.get(name, ""), str) for name in ("stdout", "stderr"))
        ):
            raise ValueError("invalid exit fault")
        rule = {**rule, "effect": effect, "max_hits": max_hits, "expires_after_seconds": expires_after}
        with self._transaction() as conn:
            current_id, _ = self._scenario(conn)
            if current_id != rule.get("scenario_id"):
                raise ValueError("wrong scenario")
            conn.execute("INSERT INTO faults VALUES (?, ?, 0, ?)", (rule_id, _json(rule), time.time() + expires_after))
        return rule_id

    def disarm(self, scenario_id, rule_id):
        with self._transaction() as conn:
            current_id, _ = self._scenario(conn)
            if current_id != scenario_id:
                raise ValueError("wrong scenario")
            if conn.execute("UPDATE faults SET expires=0 WHERE id=?", (rule_id,)).rowcount != 1:
                raise ValueError("unknown fault")
            self._checkpoint(conn, {"phase": "fault_disarmed", "rule_id": rule_id})

    @staticmethod
    def _checkpoint(conn, event):
        now = {"utc": _clock(), "monotonic": time.monotonic()}
        conn.execute(
            "INSERT INTO checkpoints(data) VALUES (?)",
            (_json({"rule_id": None, "barrier_id": None, "effect_id": None, **event, **now}),),
        )

    def _phase(self, conn, request, phase, effect_id=None):
        event = {
            "request_id": request["request_id"],
            "operation": request["operation"],
            "entity": request["entity"],
            "actor": request.get("actor", {}),
            "phase": phase,
            "effect_id": effect_id,
        }
        for rule_id, encoded, expires in conn.execute(
            "SELECT id, data, expires FROM faults WHERE expires > ? ORDER BY rowid", (time.time(),)
        ):
            rule = json.loads(encoded)
            if rule["operation"] != event["operation"] or rule["phase"] != phase:
                continue
            if any(request["entity"].get(key) != value for key, value in rule["entity"].items()):
                continue
            row = conn.execute("SELECT hits FROM faults WHERE id = ?", (rule_id,)).fetchone()
            if row[0] >= rule["max_hits"]:
                continue
            conn.execute("UPDATE faults SET hits = hits + 1 WHERE id = ?", (rule_id,))
            effect = rule["effect"]
            barrier_id = None
            if effect["kind"] == "hold":
                barrier_id = uuid4().hex
                conn.execute("INSERT INTO barriers VALUES (?, ?, NULL)", (barrier_id, rule_id))
            self._checkpoint(conn, {**event, "rule_id": rule_id, "barrier_id": barrier_id})
            return {**effect, "barrier_id": barrier_id, "expires": expires}
        self._checkpoint(conn, event)
        return None

    def begin(self, request):
        with self._transaction() as conn:
            self._scenario(conn)
            event = {**request, "utc": _clock(), "monotonic": time.monotonic()}
            conn.execute("INSERT INTO calls VALUES (?, ?, NULL)", (request["request_id"], _json(event)))
            return self._phase(conn, request, "request_received")

    def phase(self, request, phase, effect_id=None):
        with self._transaction() as conn:
            self._scenario(conn)
            return self._phase(conn, request, phase, effect_id)

    def apply(self, request, *, write, action):
        with self._transaction(write=write) as conn:
            _, world = self._scenario(conn)
            response, remote_effect = action(world)
            if remote_effect is None:
                return response, None, None
            if not write:
                raise RuntimeError("read attempted to mutate fixture")
            conn.execute("UPDATE scenario SET world = ?", (_json(world),))
            effect_id = conn.execute(
                "INSERT INTO effects(data) VALUES (?)", (_json({**request, "effect": remote_effect}),)
            ).lastrowid
            fault = self._phase(conn, request, "after_effect_commit", effect_id)
            return response, effect_id, fault

    def finish(self, request_id, outcome):
        with self._transaction() as conn:
            conn.execute("UPDATE calls SET outcome = ? WHERE id = ?", (_json(outcome), request_id))

    def failure(self, detail):
        with self._transaction() as conn:
            conn.execute("INSERT INTO failures(data) VALUES (?)", (_json(detail),))

    def release(self, barrier_id, decision):
        if decision not in {"continue", "abort"}:
            raise ValueError("invalid barrier decision")
        with self._transaction() as conn:
            row = conn.execute("SELECT decision FROM barriers WHERE id = ?", (barrier_id,)).fetchone()
            if row is None:
                raise ValueError("unknown barrier")
            if row[0] not in {None, decision}:
                raise ValueError("barrier already resolved differently")
            conn.execute("UPDATE barriers SET decision = ? WHERE id = ?", (decision, barrier_id))

    def wait(self, fault):
        barrier_id = fault["barrier_id"]
        while True:
            with self._transaction(write=False) as conn:
                decision = conn.execute("SELECT decision FROM barriers WHERE id = ?", (barrier_id,)).fetchone()[0]
            if decision:
                return decision
            if time.time() >= fault["expires"]:
                with self._transaction() as conn:
                    changed = conn.execute(
                        "UPDATE barriers SET decision = 'abort' WHERE id = ? AND decision IS NULL", (barrier_id,)
                    ).rowcount
                    if changed:
                        conn.execute(
                            "INSERT INTO failures(data) VALUES (?)",
                            (_json({"reason": "barrier_expired", "barrier_id": barrier_id}),),
                        )
                        return "abort"
            time.sleep(0.02)

    def checkpoints(self, after_seq):
        with self._transaction(write=False) as conn:
            rows = conn.execute("SELECT seq, data FROM checkpoints WHERE seq > ? ORDER BY seq", (after_seq,))
            return [{**json.loads(data), "seq": seq} for seq, data in rows]

    def state(self):
        with self._transaction(write=False) as conn:
            scenario_id, world = self._scenario(conn)
            return {
                "scenario_id": scenario_id,
                "world": world,
                "calls": [
                    {**json.loads(data), "outcome": json.loads(outcome) if outcome else None}
                    for data, outcome in conn.execute("SELECT data, outcome FROM calls ORDER BY rowid")
                ],
                "effects": [
                    {**json.loads(data), "effect_id": seq}
                    for seq, data in conn.execute("SELECT seq, data FROM effects ORDER BY seq")
                ],
                "faults": [
                    {**json.loads(data), "rule_id": key, "hits": hits, "expires": expires}
                    for key, data, hits, expires in conn.execute(
                        "SELECT id, data, hits, expires FROM faults ORDER BY rowid"
                    )
                ],
                "barriers": [
                    {"barrier_id": key, "rule_id": rule_id, "decision": decision}
                    for key, rule_id, decision in conn.execute(
                        "SELECT id, rule_id, decision FROM barriers ORDER BY rowid"
                    )
                ],
                "failures": [json.loads(row[0]) for row in conn.execute("SELECT data FROM failures ORDER BY seq")],
            }


def last_modified(world):
    return world.get("notification_last_modified") or formatdate(world["_notification_version"], usegmt=True)

"""Exercise the recovery ledger and spool consumer with real SQLite and processes."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from robert_agent.persistence import execution_control as execution_store
from robert_agent.delivery import contract as result_contract
from robert_agent.execution import spool as result_spool
from robert_agent.persistence.schema import database as storage
from robert_agent.supervision import actions as supervision_actions
from robert_agent.work.application import commands as work_items
from robert_agent.delivery.application import submission as result


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read(db, sql, parameters=()):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, parameters).fetchall()


def _database(root):
    root.mkdir()
    db = root / "recovery.sqlite3"
    storage.init_database(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            """INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root)
            VALUES('repo','example/recovery','simulation-bot','main',?,?)""",
            (str(root), str(root / "trees")),
        )
    config = {
        "repos": [{"full_name": "example/recovery"}],
        "supervision": {
            "mode": "recover_known",
            "recovery": {
                "repositories": ["example/recovery"],
                "allowed_actions": ["spool_replay", "audit_retry"],
            },
        },
    }
    return db, config


def _task(db, task_id, lineage, *, parent=None, work_item=False):
    now = _now()
    attempt = "attempt-" + task_id
    with closing(sqlite3.connect(db)) as conn, conn:
        if parent:
            (stream,) = conn.execute("SELECT workstream_id FROM tasks WHERE task_id=?", (parent,)).fetchone()
            conn.execute("UPDATE tasks SET lifecycle='completed' WHERE task_id=?", (parent,))
            conn.execute("UPDATE workstreams SET active_task_id=? WHERE workstream_id=?", (task_id, stream))
        else:
            stream = "stream-" + task_id
            conn.execute(
                """INSERT INTO workstreams(workstream_id,repo_id,active_task_id,created_at,updated_at)
                VALUES(?,'repo',?,?,?)""",
                (stream, task_id, now, now),
            )
        conn.execute(
            """INSERT INTO tasks(task_id,workstream_id,lifecycle,parent_task_id,created_at,updated_at,metadata_json)
            VALUES(?,?,'queued',?,?,?,?)""",
            (task_id, stream, parent, now, now, json.dumps({"supervision_lineage_key": lineage})),
        )
        conn.execute(
            "INSERT INTO attempts(attempt_id,task_id,attempt_no,status,metadata_json) VALUES(?,?,1,'prepared',?)",
            (attempt, task_id, json.dumps({"result_contract": result_contract.CONTRACT_ID})),
        )
        execution_store.ensure_subject(conn, "task", task_id, "human_request", now)
        if work_item:
            conn.execute(
                """INSERT INTO work_items(work_item_id,repo_id,title,origin_type,workstream_id,creation_idempotency_key,
                created_by,activated_at,created_at,updated_at) VALUES(?,'repo','Simulation','web',?,?,'operator',?,?,?)""",
                ("item-" + task_id, stream, "created-" + task_id, now, now, now),
            )
    return attempt


def _save_spool(db, task, attempt):
    # Start from the normalized pending format, as if the worker saved its
    # result immediately before losing access to SQLite.
    fingerprint = hashlib.sha256(f"{task}:{attempt}".encode()).hexdigest()
    contract = result_contract.CONTRACT_ID
    version = result_contract.NORMALIZER_VERSION
    payload = {
        "task_id": task,
        "attempt_id": attempt,
        "output_type": "comment_analysis",
        "planned_github_actions": [],
        "consumed_event_fingerprints": [],
        "used_skills": [],
        "_result_contract": contract,
        "_normalizer_version": version,
        "_submission_fingerprint": fingerprint,
    }
    with result_spool.locked_record(db, attempt) as path:
        result_spool.atomic_write(
            path,
            {
                "status": "pending",
                "result_contract": contract,
                "normalizer_version": version,
                "fingerprint": fingerprint,
                "saved_at": _now(),
                "payload": payload,
            },
        )
    return fingerprint, path


def _plan(db, config, *, task, lineage, kind, target, generation=None):
    if kind == "spool_replay":
        reason = "result_waiting_ingestion"
        evidence = {"attempt_id": target, "spool_fingerprint": generation}
    else:
        reason = "result_waiting_audit"
        evidence = {"result_id": target}
    with closing(sqlite3.connect(db)) as conn, conn:
        return supervision_actions.plan(
            conn,
            config=config,
            now=_now(),
            observations=[
                {
                    "scope_type": "task",
                    "scope_id": task,
                    "lineage_key": lineage,
                    "classification": "suspect",
                    "rule_id": "result_not_consumed",
                    "reason_code": reason,
                    "evidence": evidence,
                }
            ],
        )


def _fixture(root, db, config, *, task, kind, target, generation=None):
    path = root / "consumer.json"
    path.write_text(
        json.dumps(
            {
                "db": str(db),
                "config": config,
                "task_id": task,
                "kind": kind,
                "target_id": target,
                "generation": generation,
            }
        )
    )
    return path


def _command(mode, fixture, *arguments):
    return [sys.executable, "-B", str(Path(__file__).resolve()), "--child", mode, str(fixture), *arguments]


def _environment():
    return {
        key: value for key, value in os.environ.items() if not key.startswith(("ROBERT_", "_ROBERT_", "GH_", "GITHUB_"))
    }


def _call(mode, fixture, *arguments):
    child = subprocess.run(
        _command(mode, fixture, *arguments), env=_environment(), capture_output=True, text=True, timeout=10
    )
    if child.returncode:
        raise AssertionError(f"{mode} subprocess failed ({child.returncode}): {child.stderr}")
    return json.loads(child.stdout)


def _wait(predicate, label):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {label}")


def _race_claims(root, fixture, contender_count):
    processes = []
    try:
        for index in range(contender_count):
            processes.append(
                subprocess.Popen(
                    _command("claim_wait", fixture, str(index)),
                    env=_environment(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            )
        ready = [root / f"contender-{index}.ready" for index in range(contender_count)]
        replies = [root / f"contender-{index}.json" for index in range(contender_count)]
        _wait(lambda: all(path.exists() for path in ready), "process readiness")
        (root / "start").touch()
        _wait(lambda: all(path.exists() for path in replies), "committed claim verdicts")
        outcomes = [json.loads(path.read_text()) for path in replies]
        winners = [index for index, reply in enumerate(outcomes) if reply["status"] == "claimed"]
        assert len(winners) == 1, outcomes
        assert sum(reply["status"] == "blocked" for reply in outcomes) == contender_count - 1, outcomes
        (winner,) = winners
        assert processes[winner].poll() is None, "winner exited before crash injection"
        processes[winner].kill()
        assert processes[winner].wait(timeout=5) < 0
        for index, process in enumerate(processes):
            if index != winner:
                assert process.wait(timeout=5) == 0
        return outcomes[winner]["claim"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


def _later(value, seconds):
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def _report(name, start, checks, **facts):
    return {
        "scenario": name,
        "passed": True,
        "duration_seconds": round(time.monotonic() - start, 3),
        "checks": checks,
        **facts,
    }


def _crash(root):
    start = time.monotonic()
    db, config = _database(root)
    attempt = _task(db, "crash", "request-crash")
    fingerprint, path = _save_spool(db, "crash", attempt)
    (request,) = _plan(
        db, config, task="crash", lineage="request-crash", kind="spool_replay", target=attempt, generation=fingerprint
    )
    fixture = _fixture(root, db, config, task="crash", kind="spool_replay", target=attempt, generation=fingerprint)
    claim = _race_claims(root, fixture, 4)
    assert claim["action_id"] == request["action_id"]
    assert _read(db, "SELECT status,budget_cost,claim_generation FROM supervision_recovery_actions") == [
        ("applying", 1, 1)
    ]
    expired = _later(claim["claim_expires_at"], 1)
    verified = _call("verify", fixture, expired)
    assert verified == [{"action_id": request["action_id"], "status": "unknown"}], verified
    restarted = _call("claim", fixture, expired)
    assert restarted["status"] == "blocked", restarted
    assert _read(db, "SELECT status,budget_cost,claim_generation FROM supervision_recovery_actions") == [
        ("unknown", 1, 1)
    ]
    assert not _read(db, "SELECT result_id FROM worker_results") and path.exists()
    return _report(
        "concurrent_claim_crash_restart",
        start,
        [
            "one_claim",
            "killed_committed_consumer",
            "unknown_scope_stays_fenced",
            "budget_persists",
            "durable_input_preserved",
        ],
        contenders=4,
        winners=1,
    )


def _cancel(root):
    start = time.monotonic()
    db, config = _database(root)
    attempt = _task(db, "cancel", "request-cancel", work_item=True)
    _save_spool(db, "cancel", attempt)
    assert result.replay_pending_results(db, attempt_id=attempt)["recorded"] == 1
    [(result_id,)] = _read(db, "SELECT result_id FROM worker_results")
    _plan(db, config, task="cancel", lineage="request-cancel", kind="audit_retry", target=result_id)
    fixture = _fixture(root, db, config, task="cancel", kind="audit_retry", target=result_id)
    claim_result = _call("claim", fixture, _now())
    assert claim_result["status"] == "claimed", claim_result
    canceled = _call("cancel", fixture)
    assert canceled["ok"], canceled
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        claim = claim_result["claim"]
        now = _now()
        assert not supervision_actions.current(conn, claim, config=config, now=now)
        assert supervision_actions.finish(
            conn, claim, outcome="rejected", receipt={"reason_code": "operator_canceled"}, now=now
        )
    assert _read(db, "SELECT status,budget_cost FROM supervision_recovery_actions") == [("rejected", 0)]
    assert _read(db, "SELECT lifecycle FROM tasks WHERE task_id='cancel'") == [("canceled",)]
    assert _read(db, "SELECT json_extract(metadata_json,'$.audit') FROM worker_results") == [(None,)]
    return _report(
        "operator_cancel_fences_older_audit",
        start,
        ["real_work_item_cancel", "stale_claim_rejected", "unused_budget_released", "saved_result_preserved"],
    )


def _replay_and_successor(root):
    start = time.monotonic()
    db, config = _database(root)
    attempt = _task(db, "saved", "request-shared")
    fingerprint, path = _save_spool(db, "saved", attempt)
    (request,) = _plan(
        db, config, task="saved", lineage="request-shared", kind="spool_replay", target=attempt, generation=fingerprint
    )
    fixture = _fixture(root, db, config, task="saved", kind="spool_replay", target=attempt, generation=fingerprint)
    assert _call("replay", fixture) == {"recorded": 1, "deferred": 0, "rejected": 0}
    [(state, budget, due)] = _read(db, "SELECT status,budget_cost,next_check_at FROM supervision_recovery_actions")
    assert (state, budget) == ("observing", 1) and not path.exists()
    [(result_id, occurred_at, observed_fingerprint)] = _read(
        db, "SELECT result_id,created_at,json_extract(metadata_json,'$.submission_fingerprint') FROM worker_results"
    )
    assert observed_fingerprint == fingerprint
    assert _read(db, "SELECT result_id FROM wakeups WHERE reason='worker_result_ready'") == [(result_id,)]
    verified = _call("verify", fixture, _later(due, 1))
    assert verified == [
        {
            "action_id": request["action_id"],
            "status": "succeeded",
            "evidence": {"result_id": result_id, "attempt_id": attempt, "occurred_at": occurred_at},
        }
    ], verified
    successor = _task(db, "successor", "request-shared", parent="saved")
    new_fingerprint, new_path = _save_spool(db, "successor", successor)
    assert not _plan(
        db,
        config,
        task="successor",
        lineage="request-shared",
        kind="spool_replay",
        target=successor,
        generation=new_fingerprint,
    )
    assert result.replay_pending_results(db, config=config, attempt_id=successor) == {
        "recorded": 1,
        "deferred": 0,
        "rejected": 0,
    }
    assert not new_path.exists()
    assert _read(db, "SELECT status,budget_cost FROM supervision_recovery_actions") == [("succeeded", 1)]
    return _report(
        "durable_replay_verify_successor_budget",
        start,
        [
            "real_spool_ingestion",
            "atomic_result_and_wakeup",
            "separate_process_verification",
            "same_lineage_supervision_budget_preserved",
            "native_successor_ingestion_unblocked",
        ],
    )


def run(root):
    """Run all recovery scenarios under a caller-owned temporary directory."""
    return [
        _crash(root / "recovery-crash"),
        _cancel(root / "recovery-cancel"),
        _replay_and_successor(root / "recovery-spool"),
    ]


def _child(mode, fixture, *arguments):
    payload = json.loads(Path(fixture).read_text())
    db, config = payload.pop("db"), payload.pop("config")
    if mode == "cancel":
        context = work_items.CommandContext("operator", "simulation-operator", frozenset({"repo"}), frozenset())
        return work_items.execute_command(
            db,
            "item-" + payload["task_id"],
            context=context,
            command="cancel",
            expected_version=1,
            idempotency_key="cancel",
            notifier=lambda _: None,
        )
    if mode == "replay":
        return result.replay_pending_results(db, config=config, attempt_id=payload["target_id"])
    if mode == "verify":
        with closing(sqlite3.connect(db)) as conn, conn:
            return supervision_actions.verify(conn, observations=[], now=arguments[0])
    if mode == "claim_wait":
        root = Path(fixture).parent
        marker = root / f"contender-{arguments[0]}"
        marker.with_suffix(".ready").touch()
        _wait((root / "start").exists, "claim barrier")
        now = _now()
    else:
        assert mode == "claim", mode
        now = arguments[0]
    with closing(sqlite3.connect(db)) as conn, conn:
        verdict = supervision_actions.begin(conn, config=config, now=now, **payload)
    if mode == "claim_wait":
        temporary = marker.with_suffix(".temporary")
        temporary.write_text(json.dumps(verdict))
        temporary.replace(marker.with_suffix(".json"))
        if verdict["status"] == "claimed":
            time.sleep(30)  # The parent kills this process after the claim commits.
    return verdict


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        print(json.dumps(_child(*sys.argv[2:])))
    else:
        with tempfile.TemporaryDirectory(prefix="robert-recovery-simulation-") as directory:
            print(json.dumps(run(Path(directory)), indent=2))

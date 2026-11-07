"""Executable fault injection against real supervisor processes and temporary state.

Run with the installed project interpreter; this is not a unittest runner.
All Git repositories, credentials-free command fixtures and databases are temporary.
"""

from robert_agent.supervision.application import checks as supervision_checks
from robert_agent.supervision import health as supervision_health

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from robert_agent.execution import runtime_state as daemon_state
from robert_agent.persistence.schema import database as storage


def wait_for(predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out: " + label)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def query(db, sql, parameters=()):
    with closing(sqlite3.connect(db, timeout=0.1)) as conn:
        return conn.execute(sql, parameters).fetchall()


def process_observation(root):
    db = root / "observation.sqlite3"
    storage.init_database(db)
    config = {
        "db_path": str(db),
        "data_dir": str(root),
        "repos": [],
        "supervision": {"mode": "observe", "poll_seconds": 1, "batch_size": 3, "stall_seconds": 1},
    }
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with closing(daemon_state.connect(db)) as conn, conn:
        daemon_state.start_daemon_run(
            conn, config_path=str(root / "fixture.json"), owner_id="stalled-controller", now=old
        )
        daemon_state.acquire_daemon_lease(
            conn, resource_key="database:" + str(db.resolve()), owner_id="stalled-controller", ttl_seconds=1, now=old
        )
    config_file = root / "supervisor.json"
    config_file.write_text(json.dumps(config))
    notices = root / "notices.jsonl"
    receiver = root / "receiver.py"
    receiver.write_text(
        "import json,sys\nfrom pathlib import Path\npayload=json.load(sys.stdin)\n"
        'with Path(sys.argv[1]).open("a") as stream: stream.write(json.dumps(payload)+"\\n")\n'
    )
    env = {**os.environ, "ROBERT_NOTIFICATION_COMMAND": f"{sys.executable} {receiver} {notices}"}
    command = [
        sys.executable,
        "-c",
        "import json,sys; from robert_agent.runtime import supervisor; supervisor.run(json.load(open(sys.argv[1])))",
        str(config_file),
    ]
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(
            lambda: query(db, "SELECT incident_id FROM supervision_incidents WHERE status='open'"),
            "independent incident",
        )
        wait_for(lambda: notices.exists() and notices.stat().st_size, "independent notification")
        initial_count = query(db, "SELECT count(*) FROM supervision_incidents")[0][0]
        stamp = read_json(supervision_health.health_path(config)).get("checked_at")
        wait_for(
            lambda: read_json(supervision_health.health_path(config)).get("checked_at") != stamp, "second observation"
        )
        assert query(db, "SELECT count(*) FROM supervision_incidents")[0][0] == initial_count
        # A second OS process cannot become the same installation's observer.
        duplicate = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys; from robert_agent.runtime import supervisor; print(json.dumps(supervisor.run(json.load(open(sys.argv[1])),once=True)))",
                str(config_file),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert json.loads(duplicate.stdout)["status"] == "already_running", duplicate.stdout
        # A real half-second writer collision is visible locally but sends no outage notice.
        stop(process)
        brief_locked = root / "brief-writer.locked"
        brief = subprocess.Popen(
            [
                sys.executable,
                "-c",
                'import sqlite3,sys,time; from pathlib import Path; c=sqlite3.connect(sys.argv[1]); c.execute("BEGIN IMMEDIATE"); Path(sys.argv[2]).touch(); time.sleep(.5)',
                str(db),
                str(brief_locked),
            ]
        )
        try:
            wait_for(brief_locked.exists, "brief writer acquired")
            assert supervision_checks.check(config, record=True)["status"] == "unobservable"
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "robert_agent.runtime.supervisor",
                    "--deliver-once",
                    str(db),
                    str(supervision_health.health_path(config)),
                ],
                env=env,
                check=True,
                timeout=5,
            )
            assert not supervision_health.health_path(config).with_name("health-delivery.json").exists()
            brief.wait(timeout=3)
        finally:
            stop(brief)
        assert supervision_checks.check(config, record=True)["ok"]
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Hold a real SQLite writer in another process; observation cannot mutate business state.
        locked = root / "writer.locked"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                'import sqlite3,sys,time; from pathlib import Path; c=sqlite3.connect(sys.argv[1]); c.execute("BEGIN IMMEDIATE"); Path(sys.argv[2]).touch(); time.sleep(30)',
                str(db),
                str(locked),
            ]
        )
        try:
            wait_for(locked.exists, "writer lock acquired")
            wait_for(
                lambda: read_json(supervision_health.health_path(config)).get("status") == "unobservable",
                "DB degraded health",
            )
            wait_for(
                lambda: (
                    read_json(supervision_health.health_path(config).with_name("health-delivery.json")).get("status")
                    == "sent"
                ),
                "DB-independent notice",
            )
            assert process.poll() is None
        finally:
            stop(holder)
        wait_for(
            lambda: read_json(supervision_health.health_path(config)).get("status") == "observed", "observation resumed"
        )
        stop(process)
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        previous = read_json(supervision_health.health_path(config)).get("checked_at")
        wait_for(
            lambda: read_json(supervision_health.health_path(config)).get("checked_at") != previous,
            "restart observation",
        )
        assert query(db, "SELECT count(*) FROM supervision_incidents")[0][0] == initial_count
        assert not query(db, "SELECT action_id FROM supervision_recovery_actions")
        payloads = [json.loads(line) for line in notices.read_text().splitlines()]
        ids = [row["delivery_id"] for row in payloads]
        assert len(ids) == len(set(ids)), "acknowledged notices duplicated across restart"
        return {
            "scenario": "resident_observation_database_outage_restart",
            "passed": True,
            "incident_count": initial_count,
            "delivered_notices": len(ids),
            "checks": [
                "independent_detection",
                "exclusive_instance",
                "real_sqlite_contention",
                "brief_collision_no_notice",
                "sustained_outage_notification",
                "out_of_database_notification",
                "restart_deduplication",
                "observe_no_recovery",
            ],
        }
    finally:
        stop(process)


def diagnosis_contention(root):
    from robert_agent.supervision import diagnosis as supervision_diagnose
    from robert_agent.supervision import store as supervision_store

    db = root / "diagnosis.sqlite3"
    storage.init_database(db)
    when = datetime.now(timezone.utc)
    gate = root / "diagnosis-release"
    wrapper = root / "diagnosis-fixture.py"
    wrapper.write_text("""import json,sys,time
from pathlib import Path
packet=json.load(sys.stdin)
while not Path(sys.argv[1]).exists(): time.sleep(.01)
json.dump({'bundle_id':packet['bundle_id'],'bundle_sha256':packet['bundle_sha256'],
 'diagnosis_class':'transient_failure','evidence_ids':[packet['evidence'][0]['id']],
 'recommended_action':'wait','expected_progress':'A new controller tick'},sys.stdout)
""")
    config = {
        "db_path": str(db),
        "supervision": {
            "mode": "observe",
            "diagnosis": {"command": [sys.executable, str(wrapper), str(gate)], "timeout_seconds": 10},
        },
    }
    with closing(sqlite3.connect(db)) as conn, conn:
        supervision_store.apply_observations(
            conn,
            [
                {
                    "scope_type": "controller",
                    "scope_id": "daemon",
                    "rule_id": "controller_or_notice_stalled",
                    "classification": "confirmed",
                    "reason_code": "controller_lease_expired",
                    "severity": "error",
                    "evidence": {"daemon_run_id": "simulation-run"},
                    "expected_progress": {"kind": "controller_tick"},
                }
            ],
            when.isoformat(),
        )
    try:
        assert supervision_diagnose.poll(config, now=when)["status"] == "launched"
        wait_for(
            lambda: query(
                db,
                """SELECT 1 FROM supervision_checkpoints
            WHERE scope_type='diagnostic' AND scope_id='installation'
            AND json_extract(metadata_json,'$.claim.process_identity') IS NOT NULL""",
            ),
            "diagnostic child identity",
        )
        with closing(sqlite3.connect(db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            gate.touch()

            def pending():
                status = supervision_diagnose.poll(config, now=when)["status"]
                assert status in {"running", "completion_pending"}, status
                return status == "completion_pending"

            wait_for(pending, "reaped diagnosis retained during real SQLite writer lock")
            writer.rollback()
        outcome = supervision_diagnose.poll(config, now=when)
        assert outcome["status"] == "succeeded", outcome
        with closing(sqlite3.connect(db)) as conn:
            runner = supervision_store.get_checkpoint(conn, "diagnostic", "installation", "runner")["metadata"]
        assert runner["claim"] is None and len(runner["hourly_calls"]) == 1
        assert not query(db, "SELECT action_id FROM supervision_recovery_actions")
        return {
            "scenario": "diagnosis_reaped_result_survives_database_contention",
            "passed": True,
            "checks": [
                "real_model_protocol_process",
                "real_sqlite_writer_lock",
                "reaped_result_retry",
                "one_durable_budget_charge",
                "no_business_action",
            ],
        }
    finally:
        supervision_diagnose.shutdown(config)


def diagnosis_oversized_evidence(root):
    from robert_agent.supervision import store as supervision_store

    db = root / "diagnosis-oversized.sqlite3"
    storage.init_database(db)
    when = datetime.now(timezone.utc)
    calls = root / "diagnosis-oversized-calls.jsonl"
    wrapper = root / "diagnosis-oversized-wrapper.py"
    wrapper.write_text("""import json,sys
from pathlib import Path
raw = sys.stdin.buffer.read()
assert len(raw) <= 49152
p = json.loads(raw)
with Path(sys.argv[1]).open('a') as calls:
    print(json.dumps(p['scope']['scope_id']), file=calls)
json.dump({'bundle_id':p['bundle_id'],'bundle_sha256':p['bundle_sha256'],
    'diagnosis_class':'expected_wait','evidence_ids':[p['evidence'][0]['id']],
    'recommended_action':'wait','expected_progress':'Observe the next tick'},sys.stdout)
""")
    config = {
        "db_path": str(db),
        "supervision": {
            "mode": "observe",
            "diagnosis": {
                "command": [sys.executable, str(wrapper), str(calls)],
                "timeout_seconds": 5,
                "cooldown_seconds": 60,
                "max_calls_per_hour": 1,
            },
        },
    }

    def incident(scope, at, evidence, progress):
        with closing(sqlite3.connect(db)) as conn, conn:
            return supervision_store.apply_observations(
                conn,
                [
                    {
                        "scope_type": "controller",
                        "scope_id": scope,
                        "rule_id": "controller_or_notice_stalled",
                        "lineage_key": "shared-lineage",
                        "classification": "confirmed",
                        "reason_code": "controller_progress_stalled",
                        "severity": "error",
                        "evidence": evidence,
                        "expected_progress": progress,
                    }
                ],
                at.isoformat(),
            )[0]["incident"]["incident_id"]

    # Supplementary-plane Han characters occupy four UTF-8 bytes each.
    large = incident("large", when, {f"e{i}": "𠀀" * 1800 for i in range(6)}, {f"p{i}": "𠀀" * 700 for i in range(4)})

    launcher = """
import json,sys,time
from robert_agent.supervision import diagnosis as supervision_diagnose
config = json.loads(sys.argv[1])
try:
    while True:
        result = supervision_diagnose.poll(config, now=sys.argv[2])
        if result['status'] not in {'launched', 'running', 'completion_pending'}:
            print(json.dumps(result))
            break
        time.sleep(.02)
finally:
    supervision_diagnose.shutdown(config)
"""

    def cold_poll(at):
        result = subprocess.run(
            [sys.executable, "-c", launcher, json.dumps(config), at.isoformat()],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return json.loads(result.stdout)

    rejected = cold_poll(when)
    assert rejected == {"ok": False, "status": "evidence_too_large", "incident_id": large}, rejected
    assert not calls.exists()
    with closing(sqlite3.connect(db)) as conn:
        record = supervision_store.get_checkpoint(conn, "diagnosis", large, "analysis")["metadata"]
        assert record["bundle_bytes"] > record["byte_limit"] == 49152 and len(json.dumps(record)) < 512
        assert not supervision_store.get_checkpoint(conn, "diagnostic", "installation", "runner")

    assert cold_poll(when + timedelta(seconds=1))["status"] == "idle"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','example/fixture','bot','main','/fixture','/trees')"
        )
        conn.execute(
            "INSERT INTO workstreams(workstream_id,repo_id,created_at,updated_at) VALUES ('w','r',?,?)",
            [when.isoformat()] * 2,
        )
        conn.execute(
            "INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('small','w','running',?,?)",
            [when.isoformat()] * 2,
        )
        supervision_store.apply_observations(
            conn,
            [
                {
                    "scope_type": "task",
                    "scope_id": "small",
                    "rule_id": "worker_progress_stalled",
                    "lineage_key": "shared-lineage",
                    "classification": "suspect",
                    "severity": "warning",
                    "reason_code": "progress_stalled",
                    "evidence": {"note": "small"},
                    "expected_progress": {"kind": "worker_progress"},
                }
            ],
            when.isoformat(),
        )
    # The incident is eligible again on every resident tick. It must yield to
    # the suspect-task queue in this same bounded poll, not restart starvation.
    config["supervision"]["poll_seconds"] = 60
    outcome = cold_poll(when + timedelta(seconds=60))
    assert outcome == {"ok": True, "status": "succeeded", "candidate_id": "candidate:small:worker_progress_stalled"}, (
        outcome
    )
    assert calls.read_text().splitlines() == [json.dumps("small")]
    with closing(sqlite3.connect(db)) as conn, conn:
        runner = supervision_store.get_checkpoint(conn, "diagnostic", "installation", "runner")["metadata"]
        assert runner["claim"] is None and len(runner["hourly_calls"]) == 1
        conn.execute(
            "UPDATE supervision_incidents SET evidence_json='{}',expected_progress_json='{}' WHERE incident_id=?",
            (large,),
        )
    assert cold_poll(when + timedelta(seconds=63))["status"] == "cooldown"
    outcome = cold_poll(when + timedelta(hours=1, seconds=63))
    assert outcome == {"ok": True, "status": "succeeded", "incident_id": large}, outcome
    assert calls.read_text().splitlines() == [json.dumps("small"), json.dumps("large")]
    return {
        "scenario": "diagnosis_oversized_utf8_target_yields_to_other_targets",
        "passed": True,
        "checks": [
            "actual_utf8_byte_limit",
            "bounded_durable_rejection",
            "cold_restart",
            "same_lineage_progress",
            "suspect_candidate_at_cooldown_expiry",
            "one_call_charged",
            "reconsider_after_cooldown",
        ],
    }


def main():
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="robert-supervision-simulation-") as directory:
        root = Path(directory)
        from tests.simulations import (
            diagnosis_rate_limit_scenarios,
            model_rate_limit_scenarios,
            recovery_scenarios,
            repair_scenarios,
            service_scenarios,
        )

        scenarios = [process_observation(root), diagnosis_contention(root), diagnosis_oversized_evidence(root)]
        scenarios.extend(recovery_scenarios.run(root))
        scenarios.append(repair_scenarios.run(root))
        scenarios.append(service_scenarios.run(root))
        scenarios.extend(model_rate_limit_scenarios.run(root))
        scenarios.extend(diagnosis_rate_limit_scenarios.run(root))
        report = {
            "ok": all(item["passed"] for item in scenarios),
            "duration_seconds": round(time.monotonic() - started, 3),
            "scenarios": scenarios,
            "external_network": False,
            "production_state_used": False,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

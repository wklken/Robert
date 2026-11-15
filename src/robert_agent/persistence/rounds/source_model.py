"""Taskless identify/synthesis model ownership and shared-pool admission."""

import json
import re

from sqlalchemy import insert, select, update

from robert_agent.foundation.contracts.execution import ModelRunStatus, SourceModelExit, SourceModelStage
from robert_agent.foundation.contracts.work import SourceRoundStatus
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_sessions as read
from robert_agent.persistence.rounds import pool, source_evidence
from robert_agent.persistence.stores import source_sessions


def enqueue(
    conn,
    claim,
    *,
    run_id,
    round_id,
    request_id,
    stage,
    worker_id,
    input_version,
    prompt_sha256,
    now,
    input_packet=None,
):
    """Record a waiting model stage without a task, repository or slot."""
    if (
        stage not in (SourceModelStage.IDENTIFY, SourceModelStage.SYNTHESIS)
        or any(not isinstance(field, str) or not field.strip() for field in (run_id, worker_id, input_version))
        or not isinstance(prompt_sha256, str)
        or re.fullmatch("[0-9a-f]{64}", prompt_sha256) is None
    ):
        raise ValueError("invalid source model run")
    active = source_sessions.lock_round(conn, claim, round_id, now=now)
    if active is None or input_version != active["input_version"]:
        return None
    existing = read.model_run(conn, run_id)
    packet_json = (
        json.dumps(input_packet, ensure_ascii=False, allow_nan=False, sort_keys=True)
        if input_packet is not None
        else None
    )
    if packet_json is not None and len(packet_json.encode()) > 2 * 49152:
        raise ValueError("source model input packet exceeds storage limit")
    facts = {
        "round_id": round_id,
        "request_id": request_id,
        "stage": stage,
        "worker_id": worker_id,
        "input_version": input_version,
        "prompt_sha256": prompt_sha256,
        "input_packet_json": packet_json,
    }
    if existing is not None:
        if any(existing[key] != value for key, value in facts.items()):
            raise ValueError("source model run ID already identifies another stage")
        return existing
    if read.blockers(conn, round_id, limit=1) or not source_sessions.pending_member(conn, round_id, request_id):
        return None
    statements.write(
        conn,
        insert(tables["source_model_runs"]).values(
            run_id=run_id,
            **facts,
            status=ModelRunStatus.WAITING.value,
            created_at=now,
            updated_at=now,
        ),
    )
    return read.model_run(conn, run_id)


def _lock_run(conn, claim, run_id, now):
    if not source_sessions.lock_claim(conn, claim, now=now):
        return None
    run, round_table = (tables[name] for name in ("source_model_runs", "source_rounds"))
    return records.first(
        statements.read(
            conn,
            select(run)
            .select_from(run.join(round_table, round_table.c.round_id == run.c.round_id))
            .where(
                run.c.run_id == run_id,
                round_table.c.source_id == claim.source_id,
                round_table.c.status == SourceRoundStatus.ACTIVE.value,
            ),
        )
    )


def reserve_slot(conn, claim, run_id, *, now):
    return pool.reserve_source(conn, claim, run_id, now)


def _update(conn, row, now, **fields):
    run = tables["source_model_runs"]
    return bool(
        statements.write(
            conn,
            update(run)
            .where(run.c.run_id == row["run_id"], run.c.status == row["status"])
            .values(updated_at=now, **fields),
        )
    )


def authorize_launch(conn, claim, run_id, *, launch_id, now):
    """Durably grant a single launch; commit before the use case may spawn."""
    if not isinstance(launch_id, str) or not launch_id.strip():
        raise ValueError("invalid source model launch identity")
    row = _lock_run(conn, claim, run_id, now)
    if (
        row is None
        or row["status"] != ModelRunStatus.STARTING
        or row["slot_reserved"] != 1
        or row["launch_id"] is not None
        or source_evidence.frozen_prompt_packet(row) is None
        or not source_sessions.pending_member(conn, row["round_id"], row["request_id"])
    ):
        return False
    return _update(conn, row, now, launch_id=launch_id, launch_token=claim.token)


def record_identity(conn, claim, run_id, *, launch_id, identity, now):
    """Record the exact observed process group for a durable launch."""
    validated = source_evidence.model_identity_facts(identity)
    if validated is None:
        return False
    row = _lock_run(conn, claim, run_id, now)
    if (
        row is None
        or row["status"] not in (ModelRunStatus.STARTING, ModelRunStatus.RUNNING)
        or row["slot_reserved"] != 1
        or not launch_id
        or row["launch_id"] != launch_id
    ):
        return False
    if row["dispatch_json"] is not None:
        return source_evidence.model_identity_facts(source_evidence.json_object(row["dispatch_json"])) == validated
    return _update(conn, row, now, status=ModelRunStatus.RUNNING.value, dispatch_json=json.dumps(validated))


def finish(
    conn, claim, run_id, *, outcome, now, launch_id=None, exit_kind=None, identity=None, output=None, error=None
):
    """Persist controller-observed exit evidence before releasing a source model slot."""
    if (
        outcome not in (ModelRunStatus.SUCCEEDED, ModelRunStatus.FAILED, ModelRunStatus.INTERRUPTED)
        or (outcome == ModelRunStatus.SUCCEEDED) != isinstance(output, dict)
        or (output is not None and not isinstance(output, dict))
        or (error is not None and (not isinstance(error, str) or len(error) > 2000))
    ):
        raise ValueError("invalid source model outcome")
    row = _lock_run(conn, claim, run_id, now)
    if row is None or row["status"] not in (ModelRunStatus.WAITING, ModelRunStatus.STARTING, ModelRunStatus.RUNNING):
        return False
    if row["launch_id"] != launch_id:
        return False
    if launch_id is None:
        if (
            exit_kind not in (None, SourceModelExit.NOT_LAUNCHED)
            or outcome != ModelRunStatus.INTERRUPTED
            or row["dispatch_json"] is not None
        ):
            return False
        exit_kind = SourceModelExit.NOT_LAUNCHED.value
    elif exit_kind not in (
        SourceModelExit.GROUP_EXITED,
        SourceModelExit.SPAWN_FAILED,
        SourceModelExit.STARTUP_GROUP_CLEANED,
    ):
        return False
    elif exit_kind != SourceModelExit.GROUP_EXITED and (
        row["dispatch_json"] is not None or row["launch_token"] != claim.token
    ):
        # Direct launch outcomes come only from the granting owner in hand.
        # A takeover must recover a process identity and prove group exit.
        return False
    facts = {
        "kind": exit_kind,
        "launch_id": launch_id,
        "observed_at": now,
        "owner_token": claim.token,
        "identity": source_evidence.model_identity_facts(identity),
    }
    evidence = json.dumps(facts, sort_keys=True)
    projected = {**dict(row), "status": outcome, "slot_reserved": 0, "exit_json": evidence}
    if source_evidence.source_model_blocker(projected):
        return False
    return _update(
        conn,
        row,
        now,
        status=outcome,
        slot_reserved=0,
        exit_json=evidence,
        output_json=json.dumps(output, ensure_ascii=False, allow_nan=False) if output is not None else None,
        error=error,
    )

"""Durable stage and publication evidence for unpublished PR candidates."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update as sql_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from robert_agent.foundation.contracts.execution import ModelRunStatus
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import sqlite_master
from robert_agent.persistence.rounds import pool, review, records as round_records


SESSION = tables["pre_pr_review_sessions"]
RUN = tables["pre_pr_review_runs"]
SNAPSHOT = tables["pre_pr_review_snapshots"]
connect = review.connect
now = review.now
digest = review.digest
WRITE_STAGES = {"implement", "address_findings"}


def session(conn, *, round_id=None, task_id=None):
    if not sqlite_master.table_exists(conn, SESSION.name):
        return None
    row = round_records.session(conn, SESSION.name, task_id=task_id, round_id=round_id)
    if row is None:
        return None
    entry = review.decode(dict(row))
    entry["proof"] = json.loads(entry.pop("proof_json") or "{}")
    row = (
        records.first(
            statements.read(
                conn,
                select(SNAPSHOT.c.manifest_json).where(
                    SNAPSHOT.c.snapshot_id == entry["snapshot_id"], SNAPSHOT.c.round_id == entry["round_id"]
                ),
            )
        )
        if entry["snapshot_id"]
        else None
    )
    entry["manifest"] = json.loads(row[0]) if row else {}
    verification = entry["proof"].get("final_verification")
    entry["final_verification_run"] = None
    if isinstance(verification, dict) and verification.get("run_id"):
        run = records.first(
            statements.read(
                conn,
                select(RUN).where(
                    RUN.c.run_id == verification["run_id"],
                    RUN.c.round_id == entry["round_id"],
                ),
            )
        )
        entry["final_verification_run"] = review.decode(dict(run)) if run else None
    legacy = entry["request"].get("legacy_review_binding")
    entry["legacy_result_binding"] = None
    if isinstance(legacy, dict):
        result = tables["worker_results"]
        row = records.first(
            statements.read(
                conn,
                select(result).where(
                    result.c.result_id == legacy.get("result_id"),
                    result.c.task_id == entry["task_id"],
                    result.c.attempt_id == legacy.get("attempt_id"),
                ),
            )
        )
        if row:
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("submission_fingerprint") == legacy.get("fingerprint") and metadata.get(
                "submission_head_sha"
            ) == legacy.get("head_sha"):
                entry["legacy_result_binding"] = legacy
    return entry


def create(conn, task_id, request):
    timestamp = now()
    statements.write(
        conn,
        insert(SESSION).values(
            round_id="pre-pr-" + uuid4().hex,
            task_id=task_id,
            request_json=json.dumps(request, sort_keys=True),
            created_at=timestamp,
            updated_at=timestamp,
        ),
    )


def delete_unstarted(conn, round_id):
    return statements.write(
        conn,
        delete(SESSION).where(
            SESSION.c.round_id == round_id,
            SESSION.c.deadline_at.is_(None),
            SESSION.c.snapshot_id.is_(None),
            ~select(RUN.c.run_id).where(RUN.c.round_id == round_id).exists(),
        ),
    )


def required(conn, task_id):
    if not task_id:
        return False
    task = tables["tasks"]
    marker = func.json_extract(task.c.metadata_json, "$.pre_pr_review_required")
    row = records.first(statements.read(conn, select(marker).where(task.c.task_id == task_id)))
    return bool(row and row[0])


def bind_implementer(conn, round_id, worker):
    entry = session(conn, round_id=round_id)
    request = entry["request"]
    if not request.get("implementer_bound"):
        request["plan"]["implementer"] = worker
        request["implementer_bound"] = True
        update(conn, round_id, request_json=json.dumps(request, sort_keys=True))
    return request["plan"]


def rebind_for_retry(conn, previous, task_id, attempt_id):
    request = {**previous["request"], "retried_from_task": previous["task_id"], "retry_attempt_id": attempt_id}
    request.pop("legacy_review_binding", None)
    update(
        conn,
        previous["round_id"],
        task_id=task_id,
        request_json=json.dumps(request, sort_keys=True),
        proof_json=None,
        deadline_at=None,
        status="queued",
        error=None,
    )


def update(conn, round_id, **fields):
    if "status" in fields and fields["status"] != "retry_wait":
        fields["next_retry_at"] = None
    return round_records.update_session(conn, SESSION.name, round_id, {**fields, "updated_at": now()})


def save_snapshot(conn, round_id, manifest):
    statement = sqlite_insert(SNAPSHOT).values(
        snapshot_id=manifest["snapshot_id"],
        round_id=round_id,
        head_sha=manifest["head_sha"],
        base_sha=manifest["base_sha"],
        manifest_json=json.dumps(manifest, sort_keys=True),
        created_at=now(),
    )
    statements.write(
        conn,
        statement.on_conflict_do_update(
            index_elements=[SNAPSHOT.c.snapshot_id], set_={"manifest_json": statement.excluded.manifest_json}
        ),
    )
    current = session(conn, round_id=round_id)
    changed = current["snapshot_id"] != manifest["snapshot_id"]
    update(
        conn,
        round_id,
        snapshot_id=manifest["snapshot_id"],
        **({"report_path": None, "report_sha256": None, "proof_json": None} if changed else {}),
    )


def runs(conn, round_id):
    rows = statements.read(conn, select(RUN).where(RUN.c.round_id == round_id).order_by(RUN.c.attempt_no, RUN.c.run_id))
    return [review.decode(dict(row)) for row in rows]


def new_run(conn, round_id, stage, worker_id, owner_attempt_id, root, prompt_sha256, model=None):
    attempt = (
        statements.read(
            conn,
            select(func.coalesce(func.max(RUN.c.attempt_no), 0)).where(
                RUN.c.round_id == round_id, RUN.c.stage == stage, RUN.c.worker_id == worker_id
            ),
        )[0][0]
        + 1
    )
    run_id = "pre-pr-run-" + uuid4().hex
    directory = Path(root) / ("runs" if stage == "review" else stage)
    if stage == "review":
        directory /= hashlib.sha256(worker_id.encode()).hexdigest()[:16]
    directory /= run_id
    directory.mkdir(parents=True)
    timestamp = now()
    statements.write(
        conn,
        insert(RUN).values(
            run_id=run_id,
            round_id=round_id,
            stage=stage,
            worker_id=worker_id,
            owner_attempt_id=owner_attempt_id,
            output_dir=str(directory),
            prompt_sha256=prompt_sha256,
            attempt_no=attempt,
            model=model,
            status=ModelRunStatus.PREPARED.value,
            created_at=timestamp,
            updated_at=timestamp,
        ),
    )
    return next(row for row in runs(conn, round_id) if row["run_id"] == run_id)


def update_run(conn, run_id, **fields):
    return round_records.update_run(conn, RUN.name, run_id, {**fields, "updated_at": now()})


def reserve_slot(conn, run_id):
    run = records.first(statements.read(conn, select(RUN.c.stage).where(RUN.c.run_id == run_id)))
    if run and run[0] in WRITE_STAGES:
        # The parent task slot already accounts for this serial worker. Its
        # durable launch claim remains visible to cancellation and recovery.
        return bool(
            statements.write(
                conn,
                sql_update(RUN)
                .where(RUN.c.run_id == run_id, RUN.c.status == ModelRunStatus.WAITING.value)
                .values(status=ModelRunStatus.STARTING.value, slot_reserved=1, started_at=now(), updated_at=now()),
            )
        )
    return pool.reserve(conn, RUN.name, run_id, now())


def release_slot(conn, run_id):
    return update_run(conn, run_id, slot_reserved=0)


def reserved_runs(conn):
    return [review.decode(dict(row)) for row in statements.read(conn, select(RUN).where(RUN.c.slot_reserved == 1))]


def release_finished_slot(conn, run):
    statements.write(
        conn,
        sql_update(RUN)
        .where(RUN.c.run_id == run["run_id"], RUN.c.updated_at == run["updated_at"])
        .values(slot_reserved=0),
    )


def can_abandon(run):
    return run["status"] == ModelRunStatus.STARTING and not run["dispatch"]


def seal_result(conn, round_id, proof):
    return update(conn, round_id, proof_json=json.dumps(proof, sort_keys=True), status="ready", error=None)


def bind_result(conn, round_id, result_id):
    return update(conn, round_id, final_result_id=result_id)


def _matches_final_verification(entry, result_id=None):
    proof = entry["proof"]
    if proof.get("candidate_sha") == proof.get("head_sha"):
        return not entry.get("legacy_result_binding")
    verification = proof.get("final_verification")
    run = entry.get("final_verification_run")
    if not isinstance(verification, dict) or not run:
        legacy = entry.get("legacy_result_binding")
        return bool(
            isinstance(legacy, dict)
            and result_id
            and result_id == legacy.get("result_id")
            and entry["final_result_id"] == result_id
            and entry["task_id"] == legacy.get("task_id")
            and proof.get("attempt_id") == legacy.get("attempt_id")
            and proof.get("fingerprint") == legacy.get("fingerprint")
            and proof.get("head_sha") == legacy.get("head_sha")
            and hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest() == legacy.get("proof_sha256")
        )
    return bool(
        run["stage"] == "verify_final"
        and run["status"] == ModelRunStatus.SUCCEEDED
        and all(
            verification.get(key) == expected
            for key, expected in {
                "round_id": entry["round_id"],
                "snapshot_id": entry["snapshot_id"],
                "candidate_sha": proof.get("candidate_sha"),
                "final_sha": proof.get("head_sha"),
                "report_sha256": proof.get("report_sha256"),
                "dispositions_sha256": proof.get("dispositions_sha256"),
            }.items()
        )
        and all(
            run["artifact"].get(key) == value
            for key, value in verification.items()
            if key not in {"run_id", "worker_id"}
        )
        and verification.get("final_verification_sha256")
        and verification.get("input_sha256")
    )


def matches_submission(entry, attempt_id, fingerprint, *, head=None, result_id=None):
    proof = entry["proof"]
    return bool(
        entry["status"] == "ready"
        and entry["snapshot_id"] == proof.get("snapshot_id")
        and entry["report_sha256"] == proof.get("report_sha256")
        and proof.get("attempt_id") == attempt_id
        and fingerprint
        and proof.get("fingerprint") == fingerprint
        and (head is None or proof.get("head_sha") == head)
        and (result_id is None or entry["final_result_id"] == result_id)
        and (entry["status"] != "ready" or _matches_final_verification(entry, result_id))
    )


def task_status(conn, task_id):
    from robert_agent.security.text import safe_text

    entry = session(conn, task_id=task_id)
    if not entry:
        return None
    return {
        "round_id": entry["round_id"],
        "status": entry["status"],
        "error": safe_text(entry["error"] or ""),
        "head_sha": entry["manifest"].get("head_sha"),
        "final_sha": entry["proof"].get("head_sha"),
        "next_retry_at": entry["next_retry_at"],
        "process_pool": pool.status(conn),
        "runs": [
            {
                "run_id": run["run_id"],
                "stage": run["stage"],
                "worker_id": safe_text(run["worker_id"]),
                "model": safe_text(run["model"] or ""),
                "status": run["status"],
                "error": safe_text(run["error"] or ""),
                "attempt_no": run["attempt_no"],
                "can_abandon": can_abandon(run),
            }
            for run in runs(conn, entry["round_id"])
        ],
    }

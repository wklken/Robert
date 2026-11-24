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
    entry["completion_runs"] = (
        runs(conn, entry["round_id"])
        if entry["request"].get("plan", {}).get("review_policy") == "single_review_single_repair"
        else []
    )
    feedbacks = (
        entry["request"].get("final_feedback"),
        entry["proof"].get("decision", {}).get("final_feedback"),
        entry["request"].get("operator_reply", {}).get("decision", {}).get("final_feedback"),
    )
    feedback_ids = {
        feedback.get(field)
        for feedback in feedbacks
        if isinstance(feedback, dict)
        for field in ("source_run_id", "verification_run_id")
        if isinstance(feedback.get(field), str)
    }
    entry["feedback_runs"] = feedback_runs(conn, entry["round_id"], feedback_ids)
    reply_binding = entry["request"].get("operator_reply", {}).get("decision")
    entry["reply_context_checkpoint"] = None
    if (
        isinstance(reply_binding, dict)
        and reply_binding.get("stage") != "implement"
        and reply_binding.get("snapshot_id") != entry["snapshot_id"]
    ):
        entry["reply_context_checkpoint"] = reply_context_checkpoint(conn, entry["round_id"], reply_binding)
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
    binding = entry["proof"].get("decision") or entry["request"].get("operator_reply", {}).get("decision")
    entry["question_run"] = None
    if isinstance(binding, dict) and binding.get("source_run_id"):
        entry["question_run"] = feedback_runs(conn, entry["round_id"], {binding["source_run_id"]}).get(
            binding["source_run_id"]
        )
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
        final_result_id=None,
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


def snapshot_artifact_roots(conn, round_id):
    """Read the registered artifact roots for this round's frozen generations."""
    rows = statements.read(
        conn, select(SNAPSHOT.c.snapshot_id, SNAPSHOT.c.manifest_json).where(SNAPSHOT.c.round_id == round_id)
    )
    return {row["snapshot_id"]: json.loads(row["manifest_json"])["artifact_root"] for row in rows}


def feedback_runs(conn, round_id, run_ids):
    """Read only the registered source runs named by a final feedback binding."""
    if not run_ids:
        return {}
    rows = statements.read(conn, select(RUN).where(RUN.c.round_id == round_id, RUN.c.run_id.in_(run_ids)))
    return {row["run_id"]: review.decode(dict(row)) for row in rows}


def validate_final_feedback(entry, feedback):
    """Validate feedback bytes and their registered reviewer/address provenance."""
    error = "Final review feedback changed or has no registered source."
    try:
        identity = {
            "round_id": entry["round_id"],
            "snapshot_id": entry["snapshot_id"],
            "candidate_sha": entry["manifest"]["head_sha"],
            "report_sha256": entry["report_sha256"],
            "final_sha": feedback["final_sha"],
            "dispositions_sha256": feedback["dispositions_sha256"],
        }
        if any(feedback.get(key) != expected for key, expected in identity.items()):
            raise ValueError(error)
        registered = entry["feedback_runs"]
        verifier = registered[feedback["verification_run_id"]]
        source = registered[feedback["source_run_id"]]
        if (
            any(
                run["round_id"] != entry["round_id"]
                or not round_records.artifact_available(run)
                or run["slot_reserved"]
                for run in (verifier, source)
            )
            or verifier["stage"] != "verify_final"
            or source["stage"] != "address_findings"
        ):
            raise ValueError(error)
        artifact = verifier["artifact"]
        if (
            not feedback["verification_input_sha256"]
            or artifact.get("input_sha256") != feedback["verification_input_sha256"]
            or any(artifact.get(key) != expected for key, expected in identity.items())
            or artifact.get("final_verification_path") != feedback["verification_path"]
            or artifact.get("final_verification_sha256") != feedback["verification_sha256"]
            or source["artifact"].get("head") != feedback["final_sha"]
            or source["artifact"].get("dispositions_path") != feedback["dispositions_path"]
            or source["artifact"].get("dispositions_sha256") != feedback["dispositions_sha256"]
        ):
            raise ValueError(error)
        values = {}
        for name in ("verification", "dispositions"):
            raw = Path(feedback[name + "_path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != feedback[name + "_sha256"]:
                raise ValueError(error)
            values[name] = json.loads(raw)
        if (
            any(values["verification"].get(key) != expected for key, expected in identity.items())
            or values["verification"].get("additional_diff_sha256") != artifact.get("additional_diff_sha256")
            or any(
                values["dispositions"].get(key) != identity[key]
                for key in ("candidate_sha", "final_sha", "report_sha256")
            )
        ):
            raise ValueError(error)
        return values["verification"]
    except (KeyError, TypeError, AttributeError, OSError, ValueError) as exc:
        raise ValueError(error) from exc


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
    return run["status"] == ModelRunStatus.STARTING and not run["dispatch"].get("pid")


def seal_result(conn, round_id, proof):
    return update(conn, round_id, proof_json=json.dumps(proof, sort_keys=True), status="ready", error=None)


def bind_result(conn, round_id, result_id):
    return update(conn, round_id, final_result_id=result_id)


def _matches_final_verification(entry, result_id=None):
    proof = entry["proof"]
    policy = entry.get("request", {}).get("plan", {}).get("review_policy")
    if policy == "single_review_single_repair":
        return _matches_single_round(entry)
    if policy is not None or "review_policy" in proof or "single_round_completion" in proof:
        return False
    if proof.get("candidate_sha") == proof.get("head_sha") and not proof.get("finding_ids"):
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
        and round_records.artifact_available(run)
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


def _matches_single_round(entry):
    """The stored plan selects a completed-pass proof, never model approval."""
    proof = entry["proof"]
    marker = proof.get("single_round_completion")
    plan = entry["request"]["plan"]
    manifest = entry["manifest"]
    if (
        proof.get("review_policy") != "single_review_single_repair"
        or "final_verification" in proof
        or not isinstance(marker, dict)
        or type(marker.get("version")) is not int
        or marker.get("version") != 1
        or not isinstance(marker.get("review_run_ids"), list)
        or not marker["review_run_ids"]
    ):
        return False
    expected = {
        "round_id": entry["round_id"],
        "snapshot_id": entry["snapshot_id"],
        "candidate_sha": proof.get("candidate_sha"),
        "final_sha": proof.get("head_sha"),
        "report_sha256": entry["report_sha256"],
        "dispositions_sha256": proof.get("dispositions_sha256"),
    }
    if any(not value or marker.get(key) != value for key, value in expected.items()):
        return False
    if (
        manifest.get("snapshot_id") != expected["snapshot_id"]
        or manifest.get("head_sha") != expected["candidate_sha"]
        or proof.get("round_id") != entry["round_id"]
    ):
        return False
    try:
        history = entry.get("completion_runs", [])
        selected = {run["run_id"]: run for run in history}
        workers = [worker["name"] for worker in plan["workers"]]
        review_ids = marker["review_run_ids"]
        if len(review_ids) != len(workers) or len(set(review_ids)) != len(review_ids):
            return False
        reviews = [selected[run_id] for run_id in review_ids]
        if [run["worker_id"] for run in reviews] != workers:
            return False
        address = selected[marker["address_run_id"]]
        if address["worker_id"] != plan["implementer"]["name"]:
            return False
        synthesis = selected[marker["synthesis_run_id"]] if len(reviews) > 1 else None
        if len(reviews) == 1 and marker.get("synthesis_run_id") is not None:
            return False
        phase_runs = [("review", run) for run in reviews] + [("address_findings", address)]
        if synthesis:
            if synthesis["worker_id"] != workers[0]:
                return False
            phase_runs.append(("synthesis", synthesis))
        if any(
            run["stage"] in {"review", "synthesis", "address_findings"}
            and round_records.artifact_available(run)
            and run["run_id"] not in {source["run_id"] for _, source in phase_runs}
            for run in history
        ) or any(run["stage"] == "verify_final" for run in history):
            return False
        for stage, run in phase_runs:
            checkpoint = run.get("dispatch", {}).get("result_checkpoint", {})
            if (
                run["round_id"] != entry["round_id"]
                or run["stage"] != stage
                or not round_records.artifact_available(run)
                or run["slot_reserved"]
                or checkpoint.get("run_id") != run["run_id"]
                or checkpoint.get("prompt_sha256") != run["prompt_sha256"]
                or checkpoint.get("snapshot_id") != expected["snapshot_id"]
                or checkpoint.get("head_sha") != expected["candidate_sha"]
                or (stage == "address_findings" and checkpoint.get("worktree_head") != expected["final_sha"])
            ):
                return False
        if digest(entry["report_path"]) != expected["report_sha256"]:
            return False
        finding_ids = []
        for run in reviews:
            artifact = run["artifact"]
            for kind in ("result", "report"):
                if digest(artifact[kind + "_path"]) != artifact[kind + "_sha256"]:
                    return False
            result = json.loads(Path(artifact["result_path"]).read_text())
            if any(
                result.get(key) != value
                for key, value in {
                    "round_id": entry["round_id"],
                    "snapshot_id": expected["snapshot_id"],
                    "head_sha": expected["candidate_sha"],
                }.items()
            ):
                return False
            if not synthesis:
                finding_ids = [item["id"] for item in result["findings"] + result["dismissed_findings"]]
        if synthesis:
            artifact = synthesis["artifact"]
            if digest(artifact["evaluation_path"]) != artifact["evaluation_sha256"]:
                return False
            result = json.loads(Path(artifact["evaluation_path"]).read_text())
            if any(
                result.get(key) != value
                for key, value in {
                    "round_id": entry["round_id"],
                    "snapshot_id": expected["snapshot_id"],
                    "head_sha": expected["candidate_sha"],
                }.items()
            ):
                return False
            if not artifact.get("input_sha256"):
                return False
            finding_ids = [item["id"] for item in result["items"]]
        artifact = address["artifact"]
        if artifact.get("dispositions_sha256") != expected["dispositions_sha256"]:
            return False
        if artifact.get("head") != expected["final_sha"] or artifact.get("operator_question"):
            return False
        if digest(artifact["dispositions_path"]) != expected["dispositions_sha256"]:
            return False
        response = json.loads(Path(artifact["dispositions_path"]).read_text())
        if any(response.get(key) != expected[key] for key in ("candidate_sha", "final_sha", "report_sha256")):
            return False
        dispositions = response["dispositions"]
        disposition_ids = [item["id"] for item in dispositions]
        if (
            len(set(finding_ids)) != len(finding_ids)
            or len(set(disposition_ids)) != len(disposition_ids)
            or set(disposition_ids) != set(finding_ids)
            or proof.get("finding_ids") != disposition_ids
            or any(
                item.get("status") not in {"fixed", "dismissed", "deferred", "needs_decision"}
                or not isinstance(item.get("reason"), str)
                or not item["reason"].strip()
                for item in dispositions
            )
        ):
            return False
        required = [check for check in response["verification"] if check.get("required") is True]
        return bool(required) and all(check.get("status") == "passed" for check in required)
    except (KeyError, TypeError, AttributeError, OSError, ValueError):
        return False


def matches_submission(entry, attempt_id, fingerprint, *, head=None, result_id=None):
    proof = entry["proof"]
    return bool(
        entry["status"] in {"ready", "waiting_for_user"}
        and entry["snapshot_id"] == proof.get("snapshot_id")
        and entry["report_sha256"] == proof.get("report_sha256")
        and proof.get("attempt_id") == attempt_id
        and fingerprint
        and proof.get("fingerprint") == fingerprint
        and (
            head is None
            or proof.get("head_sha") == head
            or (entry["status"] == "waiting_for_user" and head == proof.get("decision", {}).get("candidate_sha"))
        )
        and (result_id is None or entry["final_result_id"] == result_id)
        and (entry["status"] != "ready" or _matches_final_verification(entry, result_id))
    )


def validate_reply_checkpoint(entry, binding):
    """Recheck the saved candidate and artifacts before accepting or using a reply."""
    if binding.get("stage") == "implement":
        error = "The implementation question checkpoint changed. Refresh before replying."
        run = entry.get("question_run")
        if (
            entry["round_id"] != binding.get("round_id")
            or not run
            or run["stage"] != "implement"
            or not round_records.artifact_available(run)
            or run["slot_reserved"]
            or run["artifact"].get("candidate_path") != binding.get("candidate_path")
            or run["artifact"].get("candidate_sha256") != binding.get("candidate_sha256")
            or run["dispatch"].get("result_checkpoint") != binding.get("source_checkpoint")
            or digest(binding["candidate_path"]) != binding["candidate_sha256"]
            or not binding.get("original_request")
            or digest(binding["original_request"]["path"]) != binding["original_request"]["sha256"]
        ):
            raise ValueError(error)
        return
    manifest = entry["manifest"]
    if (
        entry["round_id"] != binding["round_id"]
        or manifest.get("head_sha") != binding["candidate_sha"]
        or entry["snapshot_id"] != binding["snapshot_id"]
        or entry["report_sha256"] != binding["report_sha256"]
        or digest(entry["report_path"]) != binding["report_sha256"]
        or digest(manifest["candidate"]["candidate_path"]) != manifest["candidate"]["candidate_sha256"]
        or digest(binding["dispositions_path"]) != binding["dispositions_sha256"]
    ):
        raise ValueError("The review checkpoint changed. Refresh before replying.")
    if "source_run_id" in binding:
        run = entry.get("question_run")
        if (
            not run
            or not round_records.artifact_available(run)
            or run["slot_reserved"]
            or run["stage"] != "address_findings"
            or run["artifact"].get("dispositions_sha256") != binding["dispositions_sha256"]
        ):
            raise ValueError("The registered question source changed.")
    if "final_feedback" in binding:
        feedback = binding["final_feedback"]
        validate_final_feedback(entry, feedback)
        if any(
            feedback.get(key) != binding[key]
            for key in (
                "round_id",
                "snapshot_id",
                "candidate_sha",
                "report_sha256",
                "dispositions_path",
                "dispositions_sha256",
            )
        ):
            raise ValueError("The final review question changed. Refresh before replying.")


def reply_context_checkpoint(conn, round_id, binding):
    """Load a same-round historical snapshot and its registered report."""
    snapshot = records.first(
        statements.read(
            conn,
            select(SNAPSHOT).where(
                SNAPSHOT.c.round_id == round_id, SNAPSHOT.c.snapshot_id == binding.get("snapshot_id")
            ),
        )
    )
    artifact = tables["artifacts"]
    report = records.first(
        statements.read(
            conn,
            select(artifact.c.path, artifact.c.sha256).where(
                artifact.c.artifact_id == f"{round_id}:{binding.get('report_sha256')}",
                artifact.c.artifact_type == "pre_pr_review_report",
                artifact.c.sha256 == binding.get("report_sha256"),
            ),
        )
    )
    if not snapshot or not report:
        return None
    return {
        "round_id": snapshot["round_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "manifest": json.loads(snapshot["manifest_json"]),
        "report_path": report["path"],
        "report_sha256": report["sha256"],
    }


def validate_reply_context(entry, binding):
    """Restore only an already accepted reply as context, never stale question authority."""
    reply = entry["request"].get("operator_reply")
    if not isinstance(reply, dict) or reply.get("decision") != binding or not isinstance(binding, dict):
        raise ValueError("The reply is not bound to the authenticated review request.")
    if binding.get("stage") == "implement" or binding.get("snapshot_id") == entry["snapshot_id"]:
        validate_reply_checkpoint(entry, binding)
        return
    historical = entry.get("reply_context_checkpoint")
    if not historical or historical["round_id"] != entry["round_id"]:
        raise ValueError("The historical review context is missing. Refresh before continuing.")
    # The snapshot/report identities are historical; run metadata and file
    # bytes still come from current registered evidence and must remain intact.
    validate_reply_checkpoint({**entry, **historical}, binding)


def accept_reply(conn, entry, binding, body):
    """Bind authenticated operator context to the exact sealed waiting checkpoint."""
    proof = entry["proof"]
    expected = proof.get("decision")
    if entry["status"] != "waiting_for_user" or not expected or binding != expected:
        raise ValueError("The review question changed. Refresh before replying.")
    validate_reply_checkpoint(entry, binding)
    request = {**entry["request"], "operator_reply": {"body": body, "decision": expected, "scope": proof["scope"]}}
    if "final_feedback" in expected:
        request["final_feedback"] = expected["final_feedback"]
    update(conn, entry["round_id"], request_json=json.dumps(request, sort_keys=True))


def task_status(conn, task_id):
    entry = session(conn, task_id=task_id)
    if not entry:
        return None
    return task_status_from_records(entry, runs(conn, entry["round_id"]), pool.status(conn))


def task_status_from_records(entry, model_runs, process_pool):
    """Public status from records already loaded for this request."""
    from robert_agent.security.text import safe_text

    return {
        "round_id": entry["round_id"],
        "status": entry["status"],
        "error": safe_text(entry["error"] or ""),
        "head_sha": entry["manifest"].get("head_sha"),
        "final_sha": entry["proof"].get("head_sha"),
        "next_retry_at": entry["next_retry_at"],
        "process_pool": process_pool,
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
            for run in model_runs
        ],
    }

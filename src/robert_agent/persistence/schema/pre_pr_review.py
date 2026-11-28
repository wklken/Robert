"""Add the persisted stages for unpublished PR candidates."""

import hashlib
import json

from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from robert_agent.models.rounds import PrePrReviewRun, PrePrReviewSession, PrePrReviewSnapshot
from robert_agent.persistence.dialects.sqlite import execute_control


def _upgrade_stage_contract(conn, table, sqlite):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table.name,)).fetchone()
    if not row or all(
        stage in row[0] for stage in ("verify_final", "repair_handoff", "validate_candidate", "validate_deployment")
    ):
        return
    # Only rows physically present under the old stage contract can retain
    # their exact already-recorded result. This never authorizes a new result.
    legacy_rows = (
        conn.execute(
            "SELECT s.round_id,s.task_id,s.proof_json,s.request_json,r.result_id,r.attempt_id,r.metadata_json "
            "FROM pre_pr_review_sessions s JOIN worker_results r "
            "ON r.result_id=s.final_result_id AND r.task_id=s.task_id WHERE s.status='ready'"
        ).fetchall()
        if "verify_final" not in row[0]
        else []
    )
    for round_id, task_id, proof_json, request_json, result_id, attempt_id, metadata_json in legacy_rows:
        try:
            proof = json.loads(proof_json or "{}")
            request = json.loads(request_json)
            metadata = json.loads(metadata_json or "{}")
            if (
                not isinstance(proof, dict)
                or not isinstance(request, dict)
                or not isinstance(metadata, dict)
                or proof.get("final_verification")
                or proof.get("candidate_sha") == proof.get("head_sha")
                or proof.get("attempt_id") != attempt_id
                or not proof.get("fingerprint")
                or metadata.get("submission_fingerprint") != proof["fingerprint"]
                or not proof.get("head_sha")
                or metadata.get("submission_head_sha") != proof["head_sha"]
            ):
                continue
            request["legacy_review_binding"] = {
                "result_id": result_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "fingerprint": proof["fingerprint"],
                "head_sha": proof["head_sha"],
                "proof_sha256": hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest(),
            }
        except (ValueError, TypeError, AttributeError):
            continue
        conn.execute(
            "UPDATE pre_pr_review_sessions SET request_json=? WHERE round_id=?",
            (json.dumps(request, sort_keys=True), round_id),
        )
    replacement = table.name + "_upgrade"
    ddl = str(CreateTable(table).compile(dialect=sqlite)).replace(table.name, replacement, 1)
    execute_control(conn, ddl)
    columns = ", ".join(column.name for column in table.columns)
    execute_control(conn, f"INSERT INTO {replacement} ({columns}) SELECT {columns} FROM {table.name}")
    execute_control(conn, f"DROP TABLE {table.name}")
    execute_control(conn, f"ALTER TABLE {replacement} RENAME TO {table.name}")


def initialize(conn):
    sqlite = dialect()
    for model in (PrePrReviewSession, PrePrReviewSnapshot, PrePrReviewRun):
        table = model.__table__
        if model is PrePrReviewRun:
            _upgrade_stage_contract(conn, table, sqlite)
        execute_control(conn, str(CreateTable(table, if_not_exists=True).compile(dialect=sqlite)))
        for index in table.indexes:
            execute_control(conn, str(CreateIndex(index, if_not_exists=True).compile(dialect=sqlite)))

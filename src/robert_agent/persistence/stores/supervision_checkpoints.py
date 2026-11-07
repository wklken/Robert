"""Shared stores operations for supervision_checkpoints."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, and_, or_, not_, null, case, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_received(conn, scope_type, scope_id, _ADMISSION_RULE, value_4, now, metadata):
    stmt = insert(tables["supervision_checkpoints"]).values(
        {
            "scope_type": bindparam("p0"),
            "scope_id": bindparam("p1"),
            "rule_id": bindparam("p2"),
            "lineage_key": bindparam("p3"),
            "updated_at": bindparam("p4"),
            "metadata_json": bindparam("p5"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_id", "rule_id"],
        set_={
            "lineage_key": stmt.excluded["lineage_key"],
            "updated_at": stmt.excluded["updated_at"],
            "metadata_json": stmt.excluded["metadata_json"],
        },
        where=func.json_extract(
            tables["supervision_checkpoints"].c["metadata_json"],
            sql_constant("$.input_generation"),
        ).is_not(func.json_extract(stmt.excluded["metadata_json"], sql_constant("$.input_generation"))),
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": scope_type,
            "p1": scope_id,
            "p2": _ADMISSION_RULE,
            "p3": value_4,
            "p4": now,
            "p5": metadata,
        },
    )


def record_disposition(
    conn,
    patch,
    now,
    value_3,
    scope_type,
    scope_id,
    _ADMISSION_RULE,
    generation,
    disposition,
    patch_9,
):
    stmt = (
        update(tables["supervision_checkpoints"])
        .values(
            {
                "metadata_json": func.json_patch(
                    tables["supervision_checkpoints"].c["metadata_json"],
                    bindparam("p0"),
                ),
                "updated_at": bindparam("p1"),
                "lineage_key": func.coalesce(bindparam("p2"), tables["supervision_checkpoints"].c["lineage_key"]),
            }
        )
        .where(
            and_(
                tables["supervision_checkpoints"].c["scope_type"] == bindparam("p3"),
                tables["supervision_checkpoints"].c["scope_id"] == bindparam("p4"),
                tables["supervision_checkpoints"].c["rule_id"] == bindparam("p5"),
                func.json_extract(
                    tables["supervision_checkpoints"].c["metadata_json"],
                    sql_constant("$.input_generation"),
                )
                == bindparam("p6"),
                not_(
                    func.coalesce(
                        func.json_extract(
                            tables["supervision_checkpoints"].c["metadata_json"],
                            sql_constant("$.disposition"),
                        ),
                        sql_constant(""),
                    ).in_(
                        [
                            sql_constant("admitted_task"),
                            sql_constant("queued_native_round"),
                        ]
                    )
                ),
                or_(
                    func.coalesce(
                        func.json_extract(
                            tables["supervision_checkpoints"].c["metadata_json"],
                            sql_constant("$.disposition"),
                        ),
                        sql_constant(""),
                    )
                    != sql_constant("linked_pending"),
                    bindparam("p7") == sql_constant("admitted_task"),
                ),
                tables["supervision_checkpoints"].c["metadata_json"]
                != func.json_patch(
                    tables["supervision_checkpoints"].c["metadata_json"],
                    bindparam("p8"),
                ),
            )
        )
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": patch,
            "p1": now,
            "p2": value_3,
            "p3": scope_type,
            "p4": scope_id,
            "p5": _ADMISSION_RULE,
            "p6": generation,
            "p7": disposition,
            "p8": patch_9,
        },
    )


def update_checkpoint(conn, scope_type, scope_id, rule_id, now, value_5):
    stmt = insert(tables["supervision_checkpoints"]).values(
        {
            "scope_type": bindparam("p0"),
            "scope_id": bindparam("p1"),
            "rule_id": bindparam("p2"),
            "updated_at": bindparam("p3"),
            "metadata_json": bindparam("p4"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_id", "rule_id"],
        set_={
            "updated_at": stmt.excluded["updated_at"],
            "metadata_json": stmt.excluded["metadata_json"],
        },
        where=tables["supervision_checkpoints"].c["metadata_json"].is_not(stmt.excluded["metadata_json"]),
    )
    return statements.write(
        conn,
        stmt,
        {"p0": scope_type, "p1": scope_id, "p2": rule_id, "p3": now, "p4": value_5},
    )


def record_observation(
    conn,
    value_1,
    value_2,
    value_3,
    value_4,
    value_5,
    value_6,
    value_7,
    value_8,
    value_9,
    value_10,
    value_11,
    value_12,
):
    stmt = insert(tables["supervision_checkpoints"]).values(
        {
            "scope_type": bindparam("p0"),
            "scope_id": bindparam("p1"),
            "rule_id": bindparam("p2"),
            "lineage_key": bindparam("p3"),
            "classification": bindparam("p4"),
            "reason_code": bindparam("p5"),
            "evidence_json": bindparam("p6"),
            "progress_token": bindparam("p7"),
            "last_progress_at": bindparam("p8"),
            "next_check_at": bindparam("p9"),
            "updated_at": bindparam("p10"),
            "eligible_since": bindparam("p11"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_id", "rule_id"],
        set_={
            "last_progress_at": case(
                (
                    tables["supervision_checkpoints"].c["lineage_key"].is_not(stmt.excluded["lineage_key"]),
                    stmt.excluded["last_progress_at"],
                ),
                (
                    and_(
                        not_(stmt.excluded["progress_token"].is_(null())),
                        tables["supervision_checkpoints"].c["progress_token"].is_not(stmt.excluded["progress_token"]),
                    ),
                    stmt.excluded["last_progress_at"],
                ),
                else_=tables["supervision_checkpoints"].c["last_progress_at"],
            ),
            "progress_token": case(
                (
                    tables["supervision_checkpoints"].c["lineage_key"].is_not(stmt.excluded["lineage_key"]),
                    stmt.excluded["progress_token"],
                ),
                else_=func.coalesce(
                    stmt.excluded["progress_token"],
                    tables["supervision_checkpoints"].c["progress_token"],
                ),
            ),
            "lineage_key": stmt.excluded["lineage_key"],
            "classification": stmt.excluded["classification"],
            "reason_code": stmt.excluded["reason_code"],
            "evidence_json": stmt.excluded["evidence_json"],
            "next_check_at": stmt.excluded["next_check_at"],
            "updated_at": stmt.excluded["updated_at"],
            "eligible_since": stmt.excluded["eligible_since"],
        },
        where=or_(
            tables["supervision_checkpoints"].c["lineage_key"].is_not(stmt.excluded["lineage_key"]),
            tables["supervision_checkpoints"].c["classification"].is_not(stmt.excluded["classification"]),
            tables["supervision_checkpoints"].c["reason_code"].is_not(stmt.excluded["reason_code"]),
            tables["supervision_checkpoints"].c["evidence_json"].is_not(stmt.excluded["evidence_json"]),
            tables["supervision_checkpoints"].c["next_check_at"].is_not(stmt.excluded["next_check_at"]),
            tables["supervision_checkpoints"].c["eligible_since"].is_not(stmt.excluded["eligible_since"]),
            and_(
                not_(stmt.excluded["progress_token"].is_(null())),
                tables["supervision_checkpoints"].c["progress_token"].is_not(stmt.excluded["progress_token"]),
            ),
        ),
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": value_1,
            "p1": value_2,
            "p2": value_3,
            "p3": value_4,
            "p4": value_5,
            "p5": value_6,
            "p6": value_7,
            "p7": value_8,
            "p8": value_9,
            "p9": value_10,
            "p10": value_11,
            "p11": value_12,
        },
    )

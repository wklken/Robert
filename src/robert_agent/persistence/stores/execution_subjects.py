"""Shared stores operations for execution_subjects."""

from robert_agent.foundation.contracts.execution import BindingStatus

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, update, bindparam, and_, func, LABEL_STYLE_NONE
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_for_ensure_subject(conn, subject_type, subject_id, workflow_kind, now, now_5):
    stmt = (
        insert(tables["execution_subjects"])
        .values(
            {
                "subject_type": bindparam("p0"),
                "subject_id": bindparam("p1"),
                "workflow_kind": bindparam("p2"),
                "binding_status": sql_constant(BindingStatus.ACTIVE.value),
                "created_at": bindparam("p3"),
                "updated_at": bindparam("p4"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": subject_type,
            "p1": subject_id,
            "p2": workflow_kind,
            "p3": now,
            "p4": now_5,
        },
    )


def insert_for_import_legacy_controls(conn):
    query1 = (
        select(
            tables["workflow_bindings"].c["subject_type"],
            tables["workflow_bindings"].c["subject_id"],
            tables["workflow_bindings"].c["workflow_kind"],
            tables["workflow_bindings"].c["binding_status"],
            tables["workflow_bindings"].c["recovery_attempts"],
            tables["workflow_bindings"].c["created_at"],
            tables["workflow_bindings"].c["updated_at"],
            tables["workflow_bindings"].c["terminal_at"],
            tables["workflow_bindings"].c["metadata_json"],
        )
        .select_from(tables["workflow_bindings"])
        .where((tables["workflow_bindings"].c["owner_engine"] == sql_constant("legacy")))
        .correlate_except(tables["workflow_bindings"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    stmt = (
        insert(tables["execution_subjects"])
        .prefix_with("OR IGNORE", dialect="sqlite")
        .from_select(
            [
                "subject_type",
                "subject_id",
                "workflow_kind",
                "binding_status",
                "recovery_attempts",
                "created_at",
                "updated_at",
                "terminal_at",
                "metadata_json",
            ],
            query1,
        )
    )
    return statements.write(conn, stmt, {})


def update_for_close_binding(conn, now, now_2, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values(
            {
                "binding_status": sql_constant(BindingStatus.TERMINAL.value),
                "terminal_at": bindparam("p0"),
                "updated_at": bindparam("p1"),
            }
        )
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p2")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p3")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": now_2, "p2": subject_type, "p3": subject_id})


def mark_binding_attention(conn, now, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values({"binding_status": sql_constant(BindingStatus.ATTENTION.value), "updated_at": bindparam("p0")})
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p1")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p2")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": subject_type, "p2": subject_id})


def update_for_dispatch_queued_locked(conn, value_1, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values({"metadata_json": bindparam("p0")})
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p1")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p2")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": subject_type, "p2": subject_id})


def update_for_dispatch_queued_locked_2(conn, value_1, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values(
            {
                "binding_status": sql_constant(BindingStatus.ATTENTION.value),
                "metadata_json": bindparam("p0"),
            }
        )
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p1")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p2")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": subject_type, "p2": subject_id})


def record_recovery_creation_attention(conn, reason, value_2, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["execution_subjects"].c["metadata_json"],
                    sql_constant("$.reason_code"),
                    bindparam("p0"),
                    sql_constant("$.required_action"),
                    bindparam("p1"),
                )
            }
        )
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p2")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p3")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": reason, "p1": value_2, "p2": subject_type, "p3": subject_id})


def update_for_decisions(conn, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values(
            {
                "metadata_json": func.json_set(
                    tables["execution_subjects"].c["metadata_json"],
                    sql_constant("$.reason_code"),
                    sql_constant("migrated_event_requires_trusted_actor"),
                    sql_constant("$.required_action"),
                    sql_constant("Review migrated event authorization before retrying."),
                )
            }
        )
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p0")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p1")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": subject_type, "p1": subject_id})


def update_for_decisions_2(conn, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values(
            {
                "binding_status": sql_constant(BindingStatus.ATTENTION.value),
                "metadata_json": func.json_set(
                    tables["execution_subjects"].c["metadata_json"],
                    sql_constant("$.reason_code"),
                    sql_constant("terminal_execution_domain_mismatch"),
                    sql_constant("$.required_action"),
                    sql_constant("Inspect task state before resuming migrated events."),
                ),
            }
        )
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p0")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p1")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": subject_type, "p1": subject_id})


def update_for_classify_recovery(conn, now, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values(
            {
                "recovery_attempts": (tables["execution_subjects"].c["recovery_attempts"] + sql_constant(1)),
                "updated_at": bindparam("p0"),
            }
        )
        .where(
            and_(
                tables["execution_subjects"].c["subject_type"] == bindparam("p1"),
                tables["execution_subjects"].c["subject_id"] == bindparam("p2"),
                tables["execution_subjects"].c["binding_status"] == sql_constant(BindingStatus.ACTIVE.value),
                tables["execution_subjects"].c["recovery_attempts"] == sql_constant(0),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": now, "p1": subject_type, "p2": subject_id})


def update_for_apply_classified_fact(conn, value_1, now, subject_type, subject_id):
    stmt = (
        update(tables["execution_subjects"])
        .values({"metadata_json": bindparam("p0"), "updated_at": bindparam("p1")})
        .where(
            and_(
                (tables["execution_subjects"].c["subject_type"] == bindparam("p2")),
                (tables["execution_subjects"].c["subject_id"] == bindparam("p3")),
            )
        )
    )
    return statements.write(conn, stmt, {"p0": value_1, "p1": now, "p2": subject_type, "p3": subject_id})


def insert_for_apply(
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
):
    stmt = insert(tables["execution_subjects"]).values(
        {
            "subject_type": bindparam("p0"),
            "subject_id": bindparam("p1"),
            "workflow_kind": bindparam("p2"),
            "binding_status": bindparam("p3"),
            "recovery_attempts": bindparam("p4"),
            "created_at": bindparam("p5"),
            "updated_at": bindparam("p6"),
            "terminal_at": bindparam("p7"),
            "metadata_json": bindparam("p8"),
        }
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
        },
    )

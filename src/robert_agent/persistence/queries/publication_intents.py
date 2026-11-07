"""Shared queries operations for publication_intents."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_record_by_action_id(conn, action_id):
    query1 = (
        select(tables["publication_intents"])
        .select_from(tables["publication_intents"])
        .where((tables["publication_intents"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["publication_intents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_record_by_idempotency_key(conn, idempotency_key):
    query1 = (
        select(tables["publication_intents"])
        .select_from(tables["publication_intents"])
        .where((tables["publication_intents"].c["idempotency_key"] == bindparam("p0")))
        .correlate_except(tables["publication_intents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": idempotency_key})


def find_record_by_publication_id(conn, publication_id):
    query1 = (
        select(tables["publication_intents"])
        .select_from(tables["publication_intents"])
        .where((tables["publication_intents"].c["publication_id"] == bindparam("p0")))
        .correlate_except(tables["publication_intents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": publication_id})


def find_for_prepare_publication_intent(conn, idempotency_key, action_id):
    query1 = (
        select(tables["publication_intents"].c["action_id"])
        .select_from(tables["publication_intents"])
        .where(
            and_(
                (tables["publication_intents"].c["idempotency_key"] == bindparam("p0")),
                (tables["publication_intents"].c["action_id"] != bindparam("p1")),
            )
        )
        .correlate_except(tables["publication_intents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": idempotency_key, "p1": action_id})


def find_status_and_remote_evidence_json_by_action_id(conn, action_id):
    query1 = (
        select(
            tables["publication_intents"].c["status"],
            tables["publication_intents"].c["remote_evidence_json"],
        )
        .select_from(tables["publication_intents"])
        .where((tables["publication_intents"].c["action_id"] == bindparam("p0")))
        .correlate_except(tables["publication_intents"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": action_id})


def find_for_execute_command(conn, audit_result_id):
    table1 = tables["publication_intents"].alias("pi")
    table2 = tables["github_actions"].alias("ga")
    query3 = (
        select(sql_constant(1))
        .select_from(table1.join(table2, (table2.c["action_id"] == table1.c["action_id"]), isouter=False))
        .where((table2.c["result_id"] == bindparam("p0")))
        .correlate_except(table1, table2)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query3, {"p0": audit_result_id})

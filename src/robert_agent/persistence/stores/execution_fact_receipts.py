"""Shared stores operations for execution_fact_receipts."""

from sqlalchemy import bindparam, null
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def record_historical_facts(conn, subject_type, subject_id, fact_id, value_4):
    stmt = (
        insert(tables["execution_fact_receipts"])
        .values(
            {
                "subject_type": bindparam("p0"),
                "subject_id": bindparam("p1"),
                "fact_id": bindparam("p2"),
                "evidence_json": bindparam("p3"),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": subject_type, "p1": subject_id, "p2": fact_id, "p3": value_4})


def record_historical_facts_2(conn, subject_type, subject_id, fact_id):
    stmt = (
        insert(tables["execution_fact_receipts"])
        .values(
            {
                "subject_type": bindparam("p0"),
                "subject_id": bindparam("p1"),
                "fact_id": bindparam("p2"),
                "evidence_json": null(),
            }
        )
        .prefix_with("OR IGNORE", dialect="sqlite")
    )
    return statements.write(conn, stmt, {"p0": subject_type, "p1": subject_id, "p2": fact_id})


def insert_for_apply_classified_fact(conn, subject_type, subject_id, fact_id, value_4):
    stmt = insert(tables["execution_fact_receipts"]).values(
        {
            "subject_type": bindparam("p0"),
            "subject_id": bindparam("p1"),
            "fact_id": bindparam("p2"),
            "evidence_json": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": subject_type, "p1": subject_id, "p2": fact_id, "p3": value_4})

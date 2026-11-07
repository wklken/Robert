"""Shared queries operations for workflow_bindings."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import select, LABEL_STYLE_NONE


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def find_for_require_migrated(conn):
    query1 = (
        select(
            tables["workflow_bindings"].c["subject_type"],
            tables["workflow_bindings"].c["subject_id"],
            tables["workflow_bindings"].c["binding_status"],
        )
        .select_from(tables["workflow_bindings"])
        .where((tables["workflow_bindings"].c["owner_engine"] == sql_constant("dbos")))
        .correlate_except(tables["workflow_bindings"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {})

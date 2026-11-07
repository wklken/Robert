"""Shared filtered database operations."""

from sqlalchemy import select, bindparam, and_, LABEL_STYLE_NONE
from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def candidate_ids_from_terms_filtered(conn, items, value_1):
    query1 = (
        select(
            tables["project_memory_terms"].c["memory_id"],
            tables["project_memory_terms"].c["term_type"],
        )
        .select_from(tables["project_memory_terms"])
        .where(
            and_(
                (tables["project_memory_terms"].c["repo_id"] == bindparam("p0")),
                tables["project_memory_terms"].c["term_value"].in_(bindparam("items", expanding=True)),
            )
        )
        .correlate_except(tables["project_memory_terms"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": value_1, "items": items})

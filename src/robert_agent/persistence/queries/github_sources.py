"""Shared queries operations for github_sources."""

from functools import cache

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import (
    select,
    bindparam,
    or_,
    cast,
    Text,
    union,
    LABEL_STYLE_NONE,
)


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


@cache
def _find_source_type_and_number_and_html_url_by_source_id_statement():
    query1 = (
        select(
            tables["github_sources"].c["source_type"],
            tables["github_sources"].c["number"],
            tables["github_sources"].c["html_url"],
        )
        .select_from(tables["github_sources"])
        .where((tables["github_sources"].c["source_id"] == bindparam("p0")))
        .correlate_except(tables["github_sources"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def find_source_type_and_number_and_html_url_by_source_id(conn, origin_source_id):
    return statements.read(
        conn, _find_source_type_and_number_and_html_url_by_source_id_statement(), {"p0": origin_source_id}
    )


def find_for_source_matches(conn, identifier, identifier_2, identifier_3, like, like_5):
    query1 = (
        select(
            tables["github_sources"].c["source_id"],
            tables["github_sources"].c["source_key"],
            tables["github_sources"].c["source_type"],
            tables["github_sources"].c["number"],
            tables["github_sources"].c["html_url"],
            tables["github_sources"].c["title"],
            tables["github_sources"].c["state"],
            tables["github_sources"].c["author_login"],
            tables["github_sources"].c["source_updated_at"],
            tables["github_sources"].c["metadata_json"],
        )
        .select_from(tables["github_sources"])
        .where(
            or_(
                tables["github_sources"].c["source_key"] == bindparam("p0"),
                tables["github_sources"].c["html_url"] == bindparam("p1"),
                cast(tables["github_sources"].c["number"], Text) == bindparam("p2"),
                tables["github_sources"].c["source_key"].like(bindparam("p3")),
                tables["github_sources"].c["html_url"].like(bindparam("p4")),
            )
        )
        .order_by(
            tables["github_sources"].c["source_updated_at"].desc(),
            tables["github_sources"].c["number"].desc(),
            tables["github_sources"].c["source_key"].asc(),
        )
        .limit(sql_constant(10))
        .correlate_except(tables["github_sources"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(
        conn,
        query1,
        {
            "p0": identifier,
            "p1": identifier_2,
            "p2": identifier_3,
            "p3": like,
            "p4": like_5,
        },
    )


def find_metadata_json_and_state_by_source_id(conn, source_id):
    query1 = (
        select(
            tables["github_sources"].c["metadata_json"],
            tables["github_sources"].c["state"],
        )
        .select_from(tables["github_sources"])
        .where((tables["github_sources"].c["source_id"] == bindparam("p0")))
        .correlate_except(tables["github_sources"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": source_id})


def find_metadata_json_by_source_id(conn, source_id):
    query1 = (
        select(tables["github_sources"].c["metadata_json"])
        .select_from(tables["github_sources"])
        .where((tables["github_sources"].c["source_id"] == bindparam("p0")))
        .correlate_except(tables["github_sources"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query1, {"p0": source_id})


@cache
def _find_state_by_source_id_statement():
    query1 = (
        select(tables["github_sources"].c["state"])
        .select_from(tables["github_sources"])
        .where((tables["github_sources"].c["source_id"] == bindparam("p0")))
        .correlate_except(tables["github_sources"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    return query1


def find_state_by_source_id(conn, origin_source_id):
    return statements.read(conn, _find_state_by_source_id_statement(), {"p0": origin_source_id})


def find_for_sources_for_workstream(conn, workstream_id, workstream_id_2):
    table1 = tables["github_sources"].alias("gs")
    query3 = (
        select(tables["workstreams"].c["primary_source_id"])
        .select_from(tables["workstreams"])
        .where((tables["workstreams"].c["workstream_id"] == bindparam("p0")))
        .correlate_except(tables["workstreams"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query4 = (
        select(tables["workstream_sources"].c["source_id"])
        .select_from(tables["workstream_sources"])
        .where((tables["workstream_sources"].c["workstream_id"] == bindparam("p1")))
        .correlate_except(tables["workstream_sources"])
        .set_label_style(LABEL_STYLE_NONE)
    )
    query5 = union(query3, query4)
    query2 = (
        select(table1.c["source_type"], table1.c["number"])
        .distinct()
        .select_from(table1)
        .where(table1.c["source_id"].in_(query5))
        .order_by(table1.c["source_type"].asc(), table1.c["number"].asc())
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": workstream_id, "p1": workstream_id_2})

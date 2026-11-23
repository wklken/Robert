"""Shared queries operations for github_sources."""

from functools import cache

from robert_agent.foundation.contracts.work import WorkstreamLifecycle
from robert_agent.persistence.expressions import sql_constant
from robert_agent.persistence.queries import github_events as event_queries
from robert_agent.persistence.queries import notifications as notification_queries

from sqlalchemy import (
    select,
    bindparam,
    func,
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
        select(
            table1.c["source_type"],
            table1.c["number"],
            table1.c["provider"],
            table1.c["project_key"],
            table1.c["external_id"],
        )
        .distinct()
        .select_from(table1)
        .where(table1.c["source_id"].in_(query5))
        .order_by(table1.c["source_type"].asc(), table1.c["number"].asc(), table1.c["external_id"].asc())
        .correlate_except(table1)
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query2, {"p0": workstream_id, "p1": workstream_id_2})


def find_tapd_snapshots_for_workstream(conn, workstream_id):
    """Return each TAPD source in the workstream with its stored ticket snapshot."""
    sources = tables["github_sources"]
    members = union(
        select(tables["workstreams"].c["primary_source_id"]).where(
            tables["workstreams"].c["workstream_id"] == bindparam("p0")
        ),
        select(tables["workstream_sources"].c["source_id"]).where(
            tables["workstream_sources"].c["workstream_id"] == bindparam("p0")
        ),
    )
    query = (
        select(sources.c["html_url"], sources.c["metadata_json"])
        .where(sources.c["provider"] == sql_constant("tapd"), sources.c["source_id"].in_(members))
        .order_by(sources.c["source_key"].asc())
    )
    return statements.read(conn, query, {"p0": workstream_id})


def find_tapd_origin_for_workstream(conn, workstream_id):
    """Read the ticket owning this workstream or its directly derived pull request."""
    stream = tables["workstreams"]
    origin = stream.alias("origin")
    source, repo = tables["github_sources"], tables["repos"]
    primary = source.alias("primary_source")
    query = (
        select(
            source.c.project_key,
            source.c.external_id,
            source.c.html_url,
            source.c.source_type,
            source.c.state,
            primary.c.number,
            repo.c.full_name,
        )
        .select_from(
            stream.join(primary, primary.c.source_id == stream.c.primary_source_id)
            .join(repo, repo.c.repo_id == stream.c.repo_id)
            .outerjoin(origin, origin.c.workstream_id == stream.c.origin_workstream_id)
            .join(source, source.c.source_id == func.coalesce(origin.c.primary_source_id, stream.c.primary_source_id))
        )
        .where(stream.c.workstream_id == workstream_id, source.c.provider == "tapd")
    )
    return statements.read(conn, query)


def find_ended_tapd_ticket(conn, source_type, project_key, external_id):
    """A ticket is ended when any repository's source row stored the TAPD end state."""
    sources = tables["github_sources"]
    query = (
        select(sql_constant(1))
        .where(
            sources.c["provider"] == sql_constant("tapd"),
            sources.c["source_type"] == bindparam("p0"),
            sources.c["project_key"] == bindparam("p1"),
            sources.c["external_id"] == bindparam("p2"),
            sources.c["state"] == sql_constant("closed"),
        )
        .limit(1)
    )
    return statements.read(conn, query, {"p0": source_type, "p1": project_key, "p2": external_id})


def find_tapd_tracked_sources(conn, project_key):
    """Open TAPD sources whose workstream, work item or request is still unfinished."""
    source = tables["github_sources"].alias("gs")
    workstream = tables["workstreams"]
    work_item = tables["work_items"]
    member = tables["workstream_sources"]
    pr = tables["github_sources"].alias("pr")
    open_derived = (
        select(1)
        .select_from(workstream.join(pr, pr.c.source_id == workstream.c.primary_source_id))
        .where(
            workstream.c.origin_workstream_id == source.c.source_key,
            workstream.c.repo_id == source.c.repo_id,
            pr.c.source_type == "pull_request",
            pr.c.state == "open",
        )
        .correlate(source)
    )
    active = (
        select(sql_constant(1))
        .where(
            or_(
                workstream.c.primary_source_id == source.c.source_id,
                select(1)
                .where(
                    member.c.workstream_id == workstream.c.workstream_id,
                    member.c.source_id == source.c.source_id,
                )
                .correlate(workstream, source)
                .exists(),
            ),
            workstream.c["lifecycle"].in_(
                [
                    sql_constant(WorkstreamLifecycle.ACTIVE.value),
                    sql_constant(WorkstreamLifecycle.WAITING_FOR_USER.value),
                ]
            ),
        )
        .correlate(source)
    )
    unfinished = (
        select(sql_constant(1))
        .where(
            work_item.c["origin_source_id"] == source.c["source_id"],
            work_item.c["completed_at"].is_(None),
            work_item.c["canceled_at"].is_(None),
        )
        .correlate(source)
    )
    query = (
        select(
            source.c["source_id"],
            source.c["repo_id"],
            source.c["source_key"],
            source.c["source_type"],
            source.c["external_id"],
            source.c["metadata_json"],
        )
        .where(
            source.c["provider"] == sql_constant("tapd"),
            source.c["project_key"] == bindparam("p0"),
            source.c["state"] == sql_constant("open"),
            or_(
                active.exists(),
                open_derived.exists(),
                unfinished.exists(),
                event_queries.tapd_unprocessed_exists(source),
                notification_queries.pending_tapd_notice_exists(source),
            ),
        )
        .order_by(source.c["source_key"].asc())
        .set_label_style(LABEL_STYLE_NONE)
    )
    return statements.read(conn, query, {"p0": project_key})

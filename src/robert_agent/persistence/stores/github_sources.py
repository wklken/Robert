"""Shared stores operations for github_sources."""

from robert_agent.persistence.expressions import sql_constant

from sqlalchemy import update, bindparam, null, case, func
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def insert_fixture(conn):
    stmt = insert(tables["github_sources"]).values(
        {
            "source_id": sql_constant("source-1"),
            "repo_id": sql_constant("repo-1"),
            "source_key": sql_constant("github:x/y#1"),
            "source_type": sql_constant("issue"),
            "number": sql_constant(1),
            "html_url": sql_constant("https://github.com/x/y/issues/1"),
        }
    )
    return statements.write(conn, stmt, {})


def insert_canary_fixture(conn, source_key, source_type, number, html_url):
    stmt = insert(tables["github_sources"]).values(
        {
            "source_id": sql_constant("source-production-canary"),
            "repo_id": sql_constant("repo-production-canary"),
            "source_key": bindparam("p0"),
            "source_type": bindparam("p1"),
            "number": bindparam("p2"),
            "html_url": bindparam("p3"),
        }
    )
    return statements.write(conn, stmt, {"p0": source_key, "p1": source_type, "p2": number, "p3": html_url})


def insert_for_materialize_open_pr_workstream(conn, pr_source_id, repo_id, pr_source_key, pr_number, value_5):
    stmt = insert(tables["github_sources"]).values(
        {
            "source_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "source_key": bindparam("p2"),
            "source_type": sql_constant("pull_request"),
            "number": bindparam("p3"),
            "html_url": bindparam("p4"),
            "title": sql_constant(""),
            "state": sql_constant("open"),
            "author_login": null(),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_key"],
        set_={"html_url": stmt.excluded["html_url"], "state": sql_constant("open")},
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": pr_source_id,
            "p1": repo_id,
            "p2": pr_source_key,
            "p3": pr_number,
            "p4": value_5,
        },
    )


def insert_source_and_event(
    conn,
    source_id,
    repo_id,
    source_key,
    source_type,
    number,
    value_6,
    value_7,
    value_8,
    value_9,
    value_10,
    value_11,
):
    stmt = insert(tables["github_sources"]).values(
        {
            "source_id": bindparam("p0"),
            "repo_id": bindparam("p1"),
            "source_key": bindparam("p2"),
            "source_type": bindparam("p3"),
            "number": bindparam("p4"),
            "html_url": bindparam("p5"),
            "title": bindparam("p6"),
            "state": bindparam("p7"),
            "author_login": bindparam("p8"),
            "source_updated_at": bindparam("p9"),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_key"],
        set_={
            "html_url": func.coalesce(stmt.excluded["html_url"], tables["github_sources"].c["html_url"]),
            "title": stmt.excluded["title"],
            "state": case(
                (bindparam("p10").is_(null()), tables["github_sources"].c["state"]),
                else_=stmt.excluded["state"],
            ),
            "author_login": stmt.excluded["author_login"],
            "source_updated_at": func.coalesce(
                stmt.excluded["source_updated_at"],
                tables["github_sources"].c["source_updated_at"],
            ),
        },
    )
    return statements.write(
        conn,
        stmt,
        {
            "p0": source_id,
            "p1": repo_id,
            "p2": source_key,
            "p3": source_type,
            "p4": number,
            "p5": value_6,
            "p6": value_7,
            "p7": value_8,
            "p8": value_9,
            "p9": value_10,
            "p10": value_11,
        },
    )


def record_remote_terminal_state_locked(conn, value_1, value_2, value_3, value_4, value_5, source_id):
    stmt = (
        update(tables["github_sources"])
        .values(
            {
                "state": bindparam("p0"),
                "html_url": func.coalesce(bindparam("p1"), tables["github_sources"].c["html_url"]),
                "title": func.coalesce(
                    func.nullif(bindparam("p2"), sql_constant("")),
                    tables["github_sources"].c["title"],
                ),
                "source_updated_at": func.coalesce(bindparam("p3"), tables["github_sources"].c["source_updated_at"]),
                "metadata_json": bindparam("p4"),
            }
        )
        .where((tables["github_sources"].c["source_id"] == bindparam("p5")))
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
            "p5": source_id,
        },
    )


def record_current_state_closed_source(conn, value_1, value_2, value_3, value_4, source_id):
    stmt = (
        update(tables["github_sources"])
        .values(
            {
                "state": sql_constant("closed"),
                "html_url": func.coalesce(bindparam("p0"), tables["github_sources"].c["html_url"]),
                "title": func.coalesce(
                    func.nullif(bindparam("p1"), sql_constant("")),
                    tables["github_sources"].c["title"],
                ),
                "source_updated_at": func.coalesce(bindparam("p2"), tables["github_sources"].c["source_updated_at"]),
                "metadata_json": bindparam("p3"),
            }
        )
        .where((tables["github_sources"].c["source_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {"p0": value_1, "p1": value_2, "p2": value_3, "p3": value_4, "p4": source_id},
    )

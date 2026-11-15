"""Shared stores operations for github_sources."""

from robert_agent.persistence.expressions import sql_constant
from robert_agent.persistence.queries.repos import find_full_name_by_repo_id
from robert_agent.persistence.source_identity import github_source_host

from sqlalchemy import update, bindparam, null, case, func, and_, select
from sqlalchemy.dialects.sqlite import insert


from robert_agent.persistence.catalog import tables
from robert_agent.persistence import statements


def _identity(conn, repo_id, source_key, source_type, number, html_url):
    repo = find_full_name_by_repo_id(conn, repo_id)
    if not repo:
        raise ValueError(f"GitHub source repository {repo_id!r} is missing")
    project_key = repo[0][0]
    source = tables["sources"]
    observed_host = None
    if not html_url:
        existing = statements.read(conn, select(source.c.host).where(source.c.source_key == source_key))
        if existing:
            observed_host = existing[0][0]
    return {
        "provider": "github",
        "host": observed_host or github_source_host(project_key, source_type, number, html_url),
        "project_key": project_key,
        "external_id": str(number),
    }


def _same_identity(stmt):
    source = tables["sources"].c
    return and_(
        *(
            source[field] == stmt.excluded[field]
            for field in ("repo_id", "provider", "host", "project_key", "source_type", "external_id")
        )
    )


def _write_existing_identity(conn, stmt, parameters):
    rows = statements.read(conn, stmt.returning(tables["sources"].c.source_id), parameters)
    if not rows:
        raise ValueError("GitHub source key conflicts with an existing source identity")
    return rows[0][0]


def insert_fixture(conn):
    stmt = insert(tables["sources"]).values(
        {
            "source_id": sql_constant("source-1"),
            "repo_id": sql_constant("repo-1"),
            "source_key": sql_constant("github:x/y#1"),
            "source_type": sql_constant("issue"),
            "number": sql_constant(1),
            "html_url": sql_constant("https://github.com/x/y/issues/1"),
            **_identity(conn, "repo-1", "github:x/y#1", "issue", 1, "https://github.com/x/y/issues/1"),
        }
    )
    return statements.write(conn, stmt, {})


def insert_canary_fixture(conn, source_key, source_type, number, html_url):
    stmt = insert(tables["sources"]).values(
        {
            "source_id": sql_constant("source-production-canary"),
            "repo_id": sql_constant("repo-production-canary"),
            "source_key": bindparam("p0"),
            "source_type": bindparam("p1"),
            "number": bindparam("p2"),
            "html_url": bindparam("p3"),
            **_identity(conn, "repo-production-canary", source_key, source_type, number, html_url),
        }
    )
    return statements.write(conn, stmt, {"p0": source_key, "p1": source_type, "p2": number, "p3": html_url})


def insert_for_materialize_open_pr_workstream(conn, pr_source_id, repo_id, pr_source_key, pr_number, value_5):
    stmt = insert(tables["sources"]).values(
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
            **_identity(conn, repo_id, pr_source_key, "pull_request", pr_number, value_5),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_key"],
        set_={"html_url": stmt.excluded["html_url"], "state": sql_constant("open")},
        where=_same_identity(stmt),
    )
    return _write_existing_identity(
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
    *,
    preserve_existing=False,
):
    stmt = insert(tables["sources"]).values(
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
            **_identity(conn, repo_id, source_key, source_type, number, value_6),
        }
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source_key"],
        set_={"source_key": stmt.excluded["source_key"]}
        if preserve_existing
        else {
            "html_url": func.coalesce(stmt.excluded["html_url"], tables["sources"].c["html_url"]),
            "title": stmt.excluded["title"],
            "state": case(
                (bindparam("p10").is_(null()), tables["sources"].c["state"]),
                else_=stmt.excluded["state"],
            ),
            "author_login": stmt.excluded["author_login"],
            "source_updated_at": func.coalesce(
                stmt.excluded["source_updated_at"],
                tables["sources"].c["source_updated_at"],
            ),
        },
        where=_same_identity(stmt),
    )
    return _write_existing_identity(
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
        update(tables["sources"])
        .values(
            {
                "state": bindparam("p0"),
                "html_url": func.coalesce(bindparam("p1"), tables["sources"].c["html_url"]),
                "title": func.coalesce(
                    func.nullif(bindparam("p2"), sql_constant("")),
                    tables["sources"].c["title"],
                ),
                "source_updated_at": func.coalesce(bindparam("p3"), tables["sources"].c["source_updated_at"]),
                "metadata_json": bindparam("p4"),
            }
        )
        .where((tables["sources"].c["source_id"] == bindparam("p5")))
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
        update(tables["sources"])
        .values(
            {
                "state": sql_constant("closed"),
                "html_url": func.coalesce(bindparam("p0"), tables["sources"].c["html_url"]),
                "title": func.coalesce(
                    func.nullif(bindparam("p1"), sql_constant("")),
                    tables["sources"].c["title"],
                ),
                "source_updated_at": func.coalesce(bindparam("p2"), tables["sources"].c["source_updated_at"]),
                "metadata_json": bindparam("p3"),
            }
        )
        .where((tables["sources"].c["source_id"] == bindparam("p4")))
    )
    return statements.write(
        conn,
        stmt,
        {"p0": value_1, "p1": value_2, "p2": value_3, "p3": value_4, "p4": source_id},
    )

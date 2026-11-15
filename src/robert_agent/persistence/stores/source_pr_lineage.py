"""Materialize native PR lineage without changing the control stream or newer observations."""

import json

from sqlalchemy import func, or_, update
from sqlalchemy.dialects.sqlite import insert

from robert_agent.persistence import statements
from robert_agent.persistence.dialects.sqlite import register_timestamp_after
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.expressions import valid_json_or_empty


def insert_pr_source(conn, *, source_id, source_key, repo_id, repository, receipt, pr):
    source = tables["sources"]
    statements.write(
        conn,
        insert(source)
        .values(
            source_id=source_id,
            source_key=source_key,
            repo_id=repo_id,
            provider="github",
            host="github.com",
            project_key=repository,
            source_type="pull_request",
            number=pr["number"],
            external_id=str(pr["number"]),
            html_url=receipt.url,
            title=pr["title"],
            state=pr["state"],
            author_login=pr["author"],
            source_updated_at=receipt.observed_at,
            metadata_json=json.dumps({"native_pr_observation": pr}),
        )
        .on_conflict_do_nothing(),
    )


def insert_pr_stream(conn, *, stream_id, repo_id, source_id, origin_id, creation, now):
    stream = tables["workstreams"]
    statements.write(
        conn,
        insert(stream)
        .values(
            workstream_id=stream_id,
            repo_id=repo_id,
            primary_source_id=source_id,
            origin_workstream_id=origin_id,
            lifecycle="completed",
            created_at=now,
            updated_at=now,
            metadata_json=json.dumps({"tapd_creation": creation}, sort_keys=True),
        )
        .on_conflict_do_nothing(),
    )
    relate_pr_source(conn, stream_id=stream_id, source_id=source_id, now=now)


def attach_creation(conn, *, stream_id, repo_id, source_id, origin_id, creation):
    stream = tables["workstreams"]
    metadata = valid_json_or_empty(stream.c.metadata_json)
    return statements.write(
        conn,
        update(stream)
        .where(
            stream.c.workstream_id == stream_id,
            stream.c.repo_id == repo_id,
            stream.c.primary_source_id == source_id,
            or_(stream.c.origin_workstream_id.is_(None), stream.c.origin_workstream_id == origin_id),
            func.json_type(metadata, "$.tapd_creation").is_(None),
        )
        .values(
            origin_workstream_id=origin_id,
            metadata_json=func.json_patch(metadata, json.dumps({"tapd_creation": creation}, sort_keys=True)),
        ),
    )


def relate_pr_source(conn, *, stream_id, source_id, now):
    link = tables["workstream_sources"]
    statements.write(
        conn,
        insert(link)
        .values(
            workstream_id=stream_id,
            source_id=source_id,
            relationship="derived_pr",
            created_at=now,
        )
        .on_conflict_do_nothing(),
    )


def save_pr_observation(conn, *, source_id, pr, observed_at):
    """Keep canonical update observations distinct from the immutable creation link."""
    register_timestamp_after(conn)
    source = tables["sources"]
    metadata = valid_json_or_empty(source.c.metadata_json)
    previous = func.json_extract(metadata, "$.native_pr_update.observed_at")
    return statements.write(
        conn,
        update(source)
        .where(
            source.c.source_id == source_id,
            source.c.provider == "github",
            or_(previous.is_(None), func.python_timestamp_after(observed_at, previous) == 1),
        )
        .values(
            metadata_json=func.json_patch(
                metadata, json.dumps({"native_pr_update": {"observed_at": observed_at, "proof": pr}})
            )
        ),
    )

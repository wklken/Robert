"""Resolve canonical native PR lineage independently of current PR policy."""

from dataclasses import dataclass

from sqlalchemy import func, select

from robert_agent.foundation.contracts.delivery import GitHubActionType
from robert_agent.persistence import records, statements
from robert_agent.persistence.catalog import tables
from robert_agent.persistence.queries import source_github_publication
from robert_agent.persistence.rounds.source_evidence import json_object


@dataclass(frozen=True, slots=True)
class ConfirmedPRLineage:
    source_result_id: str
    action_id: str
    publication_id: str
    request_fingerprint: str
    origin_workstream_id: str
    workstream_id: str
    pr_source_id: str
    repo_id: str
    repository: str
    number: int
    url: str
    state: str
    creation_state: str
    pinned_base_sha: str
    head_repository: str
    head_branch: str
    head_sha: str
    base_branch: str
    author: str
    observed_at: str


def origin_workstream(conn, source_id, repo_id):
    item, stream, source = (tables[name] for name in ("work_items", "workstreams", "sources"))
    return records.first(
        statements.read(
            conn,
            select(stream.c.workstream_id)
            .select_from(
                item.join(stream, stream.c.workstream_id == item.c.workstream_id).join(
                    source, source.c.source_id == item.c.origin_source_id
                )
            )
            .where(
                item.c.origin_type == "tapd",
                item.c.origin_source_id == source_id,
                item.c.repo_id == repo_id,
                source.c.provider == "tapd",
                stream.c.primary_source_id == source_id,
                stream.c.repo_id == repo_id,
                stream.c.origin_workstream_id.is_(None),
            ),
        )
    )


def github_pr_source(conn, repository, number):
    source = tables["sources"]
    rows = statements.read(
        conn,
        select(source)
        .where(
            source.c.provider == "github",
            source.c.host == "github.com",
            func.lower(source.c.project_key) == repository.casefold(),
            source.c.source_type == "pull_request",
            source.c.external_id == str(number),
            source.c.number == number,
        )
        .limit(2),
    )
    return rows[0] if len(rows) == 1 else None


def workstream_for_pr(conn, source_id):
    stream = tables["workstreams"]
    rows = statements.read(
        conn,
        select(stream).where(stream.c.primary_source_id == source_id).limit(2),
    )
    return tuple(rows)


def pr_workstream(conn, workstream_id):
    stream = tables["workstreams"]
    return records.first(statements.read(conn, select(stream).where(stream.c.workstream_id == workstream_id)))


def task_workstream(conn, task_id):
    task = tables["tasks"]
    return records.first(statements.read(conn, select(task.c.workstream_id).where(task.c.task_id == task_id)))


def creation_evidence(receipt, prepared):
    return {
        "source_result_id": prepared["identity"]["root_id"],
        "action_id": receipt.action_id,
        "publication_id": receipt.publication_id,
        "request_fingerprint": receipt.request_fingerprint,
        "pinned_base_sha": prepared["effect"]["base_sha"],
    }


def confirmed_pr_lineage(conn, *, source_id, repo_id, workstream_id, action_id):
    """Prove the original creation association; expose mutable state separately."""
    derived = pr_workstream(conn, workstream_id)
    origin = origin_workstream(conn, source_id, repo_id)
    if (
        derived is None
        or origin is None
        or derived["origin_workstream_id"] != origin[0]
        or derived["repo_id"] != repo_id
    ):
        return None
    metadata = json_object(derived["metadata_json"]) or {}
    creation = metadata.get("tapd_creation")
    if not isinstance(creation, dict) or creation.get("action_id") != action_id:
        return None
    root_id = creation.get("source_result_id")
    confirmed = source_github_publication.confirmed_pr(conn, root_id, action_id)
    if confirmed is None:
        return None
    receipt, prepared, pr = confirmed
    identity = prepared["identity"]
    source = github_pr_source(conn, identity["repository"], pr["number"])
    if (
        identity["source_id"] != source_id
        or identity["repo_id"] != repo_id
        or identity["action_type"] != GitHubActionType.OPEN_PR
        or creation != creation_evidence(receipt, prepared)
        or task_workstream(conn, identity["task_id"]) != origin
        or pr_source_relationship(conn, workstream_id, derived["primary_source_id"]) not in ("primary", "derived_pr")
        or source is None
        or source["source_id"] != derived["primary_source_id"]
        or source["repo_id"] != repo_id
        or not same_pr_url(source["html_url"], receipt.url)
    ):
        return None
    return ConfirmedPRLineage(
        root_id,
        action_id,
        receipt.publication_id,
        receipt.request_fingerprint,
        origin[0],
        workstream_id,
        source["source_id"],
        repo_id,
        identity["repository"],
        pr["number"],
        receipt.url,
        source["state"],
        pr["state"],
        creation["pinned_base_sha"],
        pr["head_repository"],
        pr["head_branch"],
        pr["head_sha"],
        pr["base_branch"],
        pr["author"],
        receipt.observed_at,
    )


def linked_pr_lineages(conn, *, source_id, repo_id):
    """Return all durable creation links for the ticket and repository."""
    origin = origin_workstream(conn, source_id, repo_id)
    if origin is None:
        return ()
    stream = tables["workstreams"]
    derived = statements.read(
        conn,
        select(stream.c.workstream_id, stream.c.metadata_json)
        .where(stream.c.origin_workstream_id == origin[0], stream.c.repo_id == repo_id)
        .order_by(stream.c.workstream_id),
    )
    result = []
    for row in derived:
        creation = (json_object(row["metadata_json"]) or {}).get("tapd_creation")
        if isinstance(creation, dict) and isinstance(creation.get("action_id"), str):
            link = confirmed_pr_lineage(
                conn,
                source_id=source_id,
                repo_id=repo_id,
                workstream_id=row["workstream_id"],
                action_id=creation["action_id"],
            )
            if link is not None:
                result.append(link)
    return tuple(result)


def has_native_creation(conn, workstream_id):
    stream = pr_workstream(conn, workstream_id)
    return stream is not None and "tapd_creation" in (json_object(stream["metadata_json"]) or {})


def same_pr_url(actual, canonical):
    """Permit case variation only in the GitHub repository path."""
    if not isinstance(actual, str) or not isinstance(canonical, str):
        return False
    base = "https://github.com/"
    if not actual.startswith(base) or not canonical.startswith(base):
        return False
    repo, delimiter, number = canonical[len(base) :].rpartition("/pull/")
    actual_repo, observed_delimiter, observed_number = actual[len(base) :].rpartition("/pull/")
    return bool(
        repo
        and delimiter
        and number
        and observed_delimiter
        and number == observed_number
        and actual_repo.casefold() == repo.casefold()
        and not any(c in actual_repo for c in "?#")
    )


def pr_source_relationship(conn, workstream_id, source_id):
    link = tables["workstream_sources"]
    row = records.first(
        statements.read(
            conn,
            select(link.c.relationship).where(link.c.workstream_id == workstream_id, link.c.source_id == source_id),
        )
    )
    return row[0] if row is not None else None
